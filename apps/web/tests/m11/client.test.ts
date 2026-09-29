// RtClient facade (M11-FR-094, M11 §7.5, M11-AC-012 client part, M11-AC-036): in-process worker with FakeSource and real
// timers. Reference-counted subscriptions merged every 250 ms (highest rate, rate lowered on release, unsubscribe on the
// last release), the persistent TelemetryFrame and swarm views, calls with accepted/running/final and progress, batches,
// events at most once per frame, TIME with recvMs, connection info and banners across reconnects, token refresh after
// 4403 as viewer, setpoints, playback, errors, and createFakeRtClient().
import { afterEach, describe, expect, it } from 'vitest'
import { createFakeRtClient, createRtClient, type RtClientImpl } from '@/net/rt/client'
import { batchCountsOf, batchSummaryOf } from '@/net/rt/batch'
import type { CallResult, ConnInfo, ConnState, EventGap, Progress, RtErrorMsg, RtEvent, StatusItem } from '@/net/rt/types'

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))
const clients: RtClientImpl[] = []
afterEach(() => {
  for (const c of clients.splice(0)) c.close()
})

async function pump(c: RtClientImpl, ms: number, until?: () => boolean): Promise<void> {
  const end = performance.now() + ms
  while (performance.now() < end) {
    c.swapFrame()
    if (until?.()) return
    await sleep(16)
  }
}
function fake(search: string, o: { refreshToken?: (role: 'operator' | 'viewer') => Promise<string> } = {}): RtClientImpl {
  const c = createRtClient({ inProcess: true, search, refreshToken: o.refreshToken ?? (() => Promise.resolve('')) })
  clients.push(c)
  c.init({ url: 'ws://unused/api/rt', token: '', tier: 'S', deviceClass: 'software' })
  return c
}
const conn = (c: RtClientImpl) => [...c.fakeWorld()!.conns][0]

describe('subscriptions', () => {
  it('reference counting, one merged message per 250 ms window, rate follows the highest holder', async () => {
    const c = fake('?source=fake&fakeN=2')
    await pump(c, 2000, () => c.status === 'LIVE')
    expect(c.status).toBe('LIVE')
    const a1 = c.subscribe('swarm/state', { rate: 10 })
    const a2 = c.subscribe('swarm/state', { rate: 20 })
    const b1 = c.subscribe('fleet/roster', { rate: 10 })
    await pump(c, 400)
    const subs = () => conn(c).received.filter((m) => m.op === 'subscribe')
    const uns = () => conn(c).received.filter((m) => m.op === 'unsubscribe')
    expect(subs()).toHaveLength(1)
    const first = subs()[0].subs as { id: number; topic: string; rate: number }[]
    expect(first.map((s) => [s.topic, s.rate])).toEqual([['swarm/state', 20], ['fleet/roster', 10]])
    a2() // highest holder released: same id re-subscribed at 10 (no SNAPSHOT on the server)
    await pump(c, 400)
    expect(subs()).toHaveLength(2)
    expect(subs()[1].subs).toEqual([{ id: first[0].id, topic: 'swarm/state', rate: 10, mode: 'latest' }])
    a1()
    a1() // double release is a no-op
    b1()
    await pump(c, 400)
    expect(uns()).toHaveLength(1)
    expect((uns()[0].ids as number[]).sort((x, y) => x - y)).toEqual([first[0].id, first[1].id].sort((x, y) => x - y))
    // subscribe and release inside one window: nothing is sent
    const n = subs().length
    const x = c.subscribe('perf/server', { rate: 1 })
    x()
    await pump(c, 400)
    expect(subs().length).toBe(n)
  })
})

describe('frames', () => {
  it('swapFrame hands out one persistent frame; the swarm snapshot aliases its views', async () => {
    const c = fake('?source=fake&fakeN=3')
    c.subscribe('swarm/state', { rate: 20 })
    const seen = new Set<unknown>()
    let frames = 0
    const end = performance.now() + 3000
    while (performance.now() < end && !(frames > 10 && c.swarm.n === 3)) {
      const f = c.swapFrame()
      if (f) {
        seen.add(f)
        seen.add(f.swarm.pos)
        frames++
      }
      await sleep(16)
    }
    expect(frames).toBeGreaterThan(10)
    expect(seen.size).toBe(2)
    expect(c.swarm.n).toBe(3)
    expect(c.swarm.version).toBeGreaterThan(3)
    expect(Array.from(c.swarm.agentNo.subarray(0, 3))).toEqual([0, 1, 2])
    expect(Math.hypot(c.swarm.vel[0], c.swarm.vel[1])).toBeGreaterThan(0.1)
    expect(c.counters.swaps).toBe(frames)
  })

  it('onTime delivers the TIME view with a finite receive time', async () => {
    const c = fake('?source=fake&fakeN=1')
    let recv = Number.NaN
    let state = -1
    c.onTime((t, recvMs) => {
      recv = recvMs
      state = t.state
    })
    await pump(c, 2000, () => Number.isFinite(recv) && recv > 0)
    expect(state & 0x0f).toBe(9)
    expect(recv).toBeGreaterThan(0)
    expect(recv).toBeLessThanOrEqual(performance.now() + 1)
  })
})

describe('calls', () => {
  it('goto: accepted, running, progress, succeeded; the Promise is the final result', async () => {
    const c = fake('?source=fake&fakeN=1&fakeStart=0,0,40')
    c.subscribe('fleet/roster', { rate: 10 })
    await pump(c, 2000, () => c.status === 'LIVE' && c.roster.size > 0)
    const h = c.call('uav/p600-01/cmd/goto', { pos: [12, 0, 40], speed_mps: 12 })
    const seen: string[] = []
    const prog: Progress[] = []
    h.onResult((r) => seen.push(r.status))
    h.onProgress((p) => prog.push(p))
    let fin: CallResult | null = null
    void h.result.then((r) => {
      fin = r
    })
    await pump(c, 6000, () => fin !== null)
    expect(seen).toEqual(['accepted', 'running', 'succeeded'])
    expect(fin!).toMatchObject({ status: 'succeeded', final: true })
    expect(prog.length).toBeGreaterThan(0)
    expect(prog[0].data.phase).toBe('executing')
    // a late onResult gets the last result at once
    const late: string[] = []
    h.onResult((r) => late.push(r.status))
    expect(late).toEqual(['succeeded'])
  })

  it('callBatch hover on all: summary then final with counts', async () => {
    const c = fake('?source=fake&fakeN=3')
    await pump(c, 2000, () => c.status === 'LIVE')
    const h = c.callBatch('hover', '*')
    const rs: CallResult[] = []
    h.onResult((r) => rs.push(r))
    const fin = await h.result
    expect(batchSummaryOf(rs[0])).toMatchObject({ accepted_n: 3, rejected_n: 0 })
    expect(fin.status).toBe('succeeded')
    expect(batchCountsOf(fin)).toMatchObject({ succeeded: 3 })
    const body = conn(c).received.find((m) => m.op === 'call')!
    expect(body).toMatchObject({ service: 'fleet/cmd/hover', args: { vehicles: '*' }, timeout_ms: 3000 })
  })

  it('a call before init and while disconnected is rejected with 213', async () => {
    const idle = createRtClient({ inProcess: true })
    expect((await idle.call('uav/x/cmd/hover', {}).result).code).toBe(213)
    const c = fake('?source=fake&fakeN=1')
    await pump(c, 2000, () => c.status === 'LIVE')
    c.fakeWorld()!.closeAll(1011)
    await pump(c, 200, () => c.status === 'RECONNECTING')
    const r = await c.call('uav/p600-01/cmd/hover', {}).result
    expect([r.status, r.code, r.final]).toEqual(['rejected', 213, true])
  })

  it('playback resolves with the playbackState of the same request id (213 in D1-core)', async () => {
    const c = fake('?source=fake&fakeN=1')
    await pump(c, 2000, () => c.status === 'LIVE')
    const states: string[] = []
    c.onPlaybackState((s) => states.push(s.status))
    const s = await c.playback('open', { run: 'r1', segment: 0 })
    expect(s).toMatchObject({ status: 'error', code: 213 })
    expect(states).toEqual(['error'])
  })
})

describe('events, status and connection', () => {
  it('events arrive at most once per frame while a frame loop runs', async () => {
    const c = createFakeRtClient({ n: 2, eventPeriodS: 0.05 })
    clients.push(c)
    c.subscribe('event', { rate: 0, mode: 'all' })
    let batches = 0
    let events = 0
    c.onEvents((b) => {
      batches++
      events += b.length
      expect(b.length).toBeGreaterThan(0)
    })
    const before = c.counters.swaps
    let loops = 0
    await pump(c, 1500, () => {
      loops++
      return false
    })
    expect(events).toBeGreaterThan(10)
    expect(batches).toBeLessThanOrEqual(loops + 1)
    expect(c.counters.swaps - before).toBeGreaterThan(0)
  })

  it('reconnect: RECONNECTING with timestamps, banners re-sent and the removed one gone', async () => {
    const c = fake('?source=fake&fakeN=1')
    const states: [ConnState, ConnInfo][] = []
    c.onConnState((s, i) => states.push([s, { ...i }]))
    let banners: readonly StatusItem[] = []
    c.onStatus((items) => {
      banners = items
    })
    await pump(c, 2000, () => c.status === 'LIVE')
    const w = c.fakeWorld()!
    w.setStatus('a', 'warning', 'banner a')
    w.setStatus('b', 'info', 'banner b')
    await pump(c, 300, () => banners.length === 2)
    expect(banners.map((b) => b.id).sort()).toEqual(['a', 'b'])
    w.closeAll(1011)
    w.removeStatus('b') // removed while this client is away
    await pump(c, 300, () => c.status === 'RECONNECTING')
    const rc = states.find(([s]) => s === 'RECONNECTING')![1]
    expect(rc.code).toBe(1011)
    expect(rc.sinceMs).toBeGreaterThan(0)
    expect(rc.lastLiveMs).toBeGreaterThan(0)
    await pump(c, 3000, () => c.status === 'LIVE')
    await pump(c, 300)
    expect(c.status).toBe('LIVE')
    expect(banners.map((b) => b.id)).toEqual(['a'])
    expect(c.statusList().map((b) => b.id)).toEqual(['a'])
  })

  it('4403: the client asks for a viewer token and reconnects', async () => {
    const roles: string[] = []
    const c = fake('?source=fake&fakeN=1', { refreshToken: (role) => {
      roles.push(role)
      return Promise.resolve('viewer-token')
    } })
    await pump(c, 2000, () => c.status === 'LIVE')
    c.fakeWorld()!.closeAll(4403)
    await pump(c, 2000, () => roles.length > 0 && c.status === 'LIVE')
    expect(roles).toEqual(['viewer'])
    expect(c.status).toBe('LIVE')
    expect(c.connInfo.error).toBeUndefined()
  })

  it('setpoints reach the vehicle through CLIENT_DATA; errors surface through onError', async () => {
    const c = fake('?source=fake&fakeN=1&fakeStart=0,0,30')
    c.subscribe('fleet/roster', { rate: 10 })
    const errors: RtErrorMsg[] = []
    c.onError((e) => errors.push(e))
    await pump(c, 2000, () => c.status === 'LIVE' && c.roster.size > 0)
    c.publishSetpoint(0, 1, 0, 0, 0)
    await pump(c, 200, () => errors.length > 0)
    expect(errors[0]).toMatchObject({ code: 322 })
    const h = c.call('uav/p600-01/cmd/velocity', { frame: 'world' })
    await pump(c, 60, () => false) // accepted; the first setpoint must follow within the 250 ms watchdog
    const t0 = performance.now()
    await pump(c, 800, () => {
      c.publishSetpoint(0, 5, 0, 0, 0)
      return performance.now() - t0 > 600
    })
    expect(c.fakeWorld()!.positionOf(0)[0]).toBeGreaterThan(1.5)
    const fin = await h.result // the watchdog ends it once we stop
    expect([fin.status, fin.code]).toEqual(['canceled', 209])
  })

  it('local event gaps are reported through onEventGap', async () => {
    const c = fake('?source=fake&fakeN=1&eventPeriodS=3600')
    c.subscribe('event', { rate: 0, mode: 'all' })
    const gaps: EventGap[] = []
    const got: RtEvent[] = []
    c.onEventGap((g) => gaps.push(g))
    c.onEvents((b) => got.push(...b))
    await pump(c, 2000, () => c.status === 'LIVE')
    await pump(c, 300)
    c.setHidden(true)
    c.fakeWorld()!.emitStorm(9000)
    await sleep(400)
    c.setHidden(false)
    await pump(c, 500, () => gaps.length > 0)
    expect(gaps).toHaveLength(1)
    expect(gaps[0].dropped).toBeGreaterThanOrEqual(9000 - 8192)
    expect(got.length).toBeLessThanOrEqual(8192)
  })
})
