// net/rt codec and state machine (M11 §6.3.8, §6.4.14, §6.6.3; AWR-17 §6.4, §6.9, §6.10): slot layout, header round
// trip, TIME encode/decode through the host, credit ack cadence, ClockSync, backoff, close codes, fake URL switch.
import { describe, expect, it } from 'vitest'
import * as L from '@awr/contracts/layouts'
import { newTimeView, readTime, writeTime } from '@awr/contracts/time'
import { ClockSync } from '@/net/rt/clockSync'
import { H, REGION, SF, SLOT_BYTES, SLOT_CAP, SLOT_FULL, SLOT_ITEM, SLOT_RAW } from '@/net/rt/frame'
import { parseFakeUrl } from '@/net/rt/FakeSource'
import { resolveRtUrl } from '@/net/rt/client'
import { BACKOFF, backoffDelay, protocolsFor } from '@/net/rt/transport'
import { ctrlOps, rig } from './harness'

describe('TelemetryFrame slot layout (M11 §6.3.8)', () => {
  it('regions are contiguous and fit in 64 KiB', () => {
    expect(REGION.swarmPos).toBe(256)
    expect(REGION.swarmQuat - REGION.swarmPos).toBe(12 * SLOT_CAP)
    expect(REGION.swarmVel - REGION.swarmQuat).toBe(16 * SLOT_CAP)
    expect(REGION.swarmAgentNo - REGION.swarmVel).toBe(12 * SLOT_CAP)
    expect(REGION.swarmFs - REGION.swarmAgentNo).toBe(2 * SLOT_CAP)
    expect(REGION.full - REGION.swarmFs).toBe(4 * SLOT_CAP)
    expect(REGION.raw - REGION.full).toBe(SLOT_ITEM * SLOT_FULL)
    expect(REGION.end - REGION.raw).toBe(SLOT_ITEM * SLOT_RAW)
    expect(REGION.end).toBeLessThanOrEqual(SLOT_BYTES)
    expect(H.eventGaps + 4).toBeLessThanOrEqual(256)
  })
})

describe('TIME frame (AWR-17 §6.4)', () => {
  it('round trips state, bit 7, epoch and 64-bit times below 2^53', () => {
    const b = new DataView(new ArrayBuffer(24))
    const v = { state: 9, replay: true, epoch: 65535, rate: 10, t_sim_ns: 2 ** 52 + 12345, t_srv_ns: 123456789012 }
    writeTime(b, 0, v)
    expect(b.getUint8(1)).toBe(0x89)
    const r = readTime(b, 0, newTimeView())
    expect(r).toEqual(v)
  })
})

describe('rt.worker engine', () => {
  it('handshake: hello after serverInfo, SYNCING then LIVE on the first SNAPSHOT, header fields in slots', async () => {
    const r = await rig({ url: 'fake:?n=3', n: 3 })
    r.pull() // the first pull flushes the control messages that arrived with the handshake
    expect(ctrlOps(r.ctrl, 'serverInfo')).toHaveLength(1)
    // without subscriptions the first TIME after hello already makes the link LIVE
    expect(['SYNCING', 'LIVE']).toContain(r.host.connState)
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 20, mode: 'latest' }, { id: 2, topic: 'fleet/roster', rate: 10, mode: 'latest' },
      { id: 3, topic: 'uav/uav0002/state', rate: 60, mode: 'latest' }, { id: 4, topic: 'env/state', rate: 10, mode: 'latest' }] })
    for (let k = 0; k < 12; k++) {
      await r.step(1000 / 60)
      r.pull()
    }
    expect(r.host.connState).toBe('LIVE')
    const conn = ctrlOps(r.ctrl, 'conn').map((m) => m.state)
    expect(conn).toEqual(['CONNECTING', 'SYNCING', 'LIVE'])
    const withSwarm = r.slots.filter((s) => s.hdr.swarmN > 0)
    expect(withSwarm.length).toBeGreaterThan(2)
    const s = withSwarm[withSwarm.length - 1]
    expect(s.hdr.swarmN).toBe(3)
    expect(s.hdr.epoch).toBe(1)
    expect(Array.from(s.swarm.agentNo.subarray(0, 3))).toEqual([0, 1, 2])
    expect(r.slots.some((x) => (x.hdr.flags & SF.SNAPSHOT) !== 0)).toBe(true)
    expect(r.slots.some((x) => (x.hdr.flags & SF.ROSTER_CHANGED) !== 0)).toBe(true)
    // Full64 of uav0002 copied with agent number from the payload
    const full = r.slots.find((x) => x.hdr.fullCount > 0)!
    expect(full.fullAgentNo(0)).toBe(1)
    // roster and env keyframe on the control path
    expect(ctrlOps(r.ctrl, 'roster')[0].entries).toHaveLength(3)
    const env = ctrlOps(r.ctrl, 'data').find((m) => m.topic === 'env/state')!
    expect((env.data as { schema: string }).schema).toBe('awr.env.keyframe.v1')
  })

  it('acks consumed frames every 3 frames or 50 ms (credit, AWR-17 §6.9)', async () => {
    const r = await rig({ url: 'fake:?n=1', window: 3 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 60, mode: 'latest' }] })
    r.pull()
    for (let k = 0; k < 120; k++) {
      await r.step(1000 / 60)
      r.pull()
    }
    // with window 3 the fake would stall after 3 frames without acks; steady delivery proves the acks
    expect(r.fake.stats.frames).toBeGreaterThan(60)
    expect(r.slots.filter((s) => s.hdr.swarmN > 0).length).toBeGreaterThan(60)
  })

  it('60 Hz channel arrival rate and jitter are measured in the worker, independent of the pull rate (FX2-R3)', async () => {
    const run = async (pullEvery: number): Promise<{ hz: number; jitter: number; skips: number }> => {
      const r = await rig({ url: 'fake:?n=3', n: 3, window: 6 })
      r.pull()
      r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 10, mode: 'latest' }, { id: 3, topic: 'uav/uav0002/state', rate: 60, mode: 'latest' }] })
      for (let k = 0; k < 180; k++) {
        await r.step(1000 / 60)
        if (k % pullEvery === pullEvery - 1) r.pull()
      }
      const s = r.slots[r.slots.length - 1]
      return { hz: s.hdr.selHz, jitter: s.hdr.selJitterMs, skips: r.fake.stats.creditSkips }
    }
    expect(H.selJitterMs + 4).toBeLessThanOrEqual(256)
    // 30 fps main thread: the credit window (W = 6) never fills, the worker sees the 60 Hz channel at 60 Hz
    const a = await run(2)
    expect(a.hz).toBeGreaterThan(55)
    expect(a.jitter).toBeLessThan(1000 / 60)
    expect(a.skips).toBe(0)
    // 20 fps main thread: one ack per pull at hand-over leaves the 60 Hz channel inside the credit window (D1-AC-26)
    const c = await run(3)
    expect(c.hz).toBeGreaterThan(55)
    expect(c.skips).toBeLessThanOrEqual(2)
    // 10 fps main thread: credit stalls are visible as arrival jitter (no longer the main thread's frame times)
    const b = await run(6)
    expect(b.hz).toBeGreaterThan(20)
    expect(Number.isFinite(b.jitter)).toBe(true)
  })

  it('ClockSync: 5 pings 100 ms apart then 2 Hz; offset and srtt in the slot header', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    r.pull()
    for (let k = 0; k < 60; k++) {
      await r.step(1000 / 60)
      r.pull()
    }
    expect(r.fake.stats.pings).toBeGreaterThanOrEqual(5)
    expect(r.fake.stats.pings).toBeLessThanOrEqual(9)
    const s = r.slots[r.slots.length - 1]
    // fake server clock = now - origin(1000 ms at construction); main and worker share time origin 0 here
    expect(s.hdr.clockOffsetMainMs).toBeCloseTo(-1000, 0)
    expect(s.hdr.srttMs).toBeGreaterThanOrEqual(0)
  })

  it('close codes: backoff reconnect, 4426 fatal, 4401 asks for a token', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    r.pull()
    await r.step(20)
    r.fake.serverClose(1011)
    await r.step(1)
    r.pull()
    expect(r.host.connState).toBe('RECONNECTING')
    const rc = ctrlOps(r.ctrl, 'conn').at(-1)!
    expect(rc.nextInMs).toBe(backoffDelay(1, 0.5))
    r.clock.t += BACKOFF.minDelayMs + 1
    r.runTimers()
    await r.step(1)
    expect(['CONNECTING', 'SYNCING', 'LIVE']).toContain(r.host.connState)
    r.host.socket!.close(4401)
    await r.step(1)
    r.pull()
    expect(ctrlOps(r.ctrl, 'needToken')).toHaveLength(1)
    r.host.onMessage({ cmd: 'token', token: 'tok' })
    await r.step(1)
    expect(['CONNECTING', 'SYNCING', 'LIVE']).toContain(r.host.connState)
    r.host.socket!.close(4426)
    await r.step(1)
    expect(r.host.connState).toBe('FATAL')
  })

  it('calls while disconnected are rejected at once; call timeout synthesises timeout 200', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    r.fake.serverClose(1011)
    await r.step(1)
    r.host.onMessage({ cmd: 'call', id: 'x', service: 'uav/p600-01/cmd/hover', args: {}, timeoutMs: 100 })
    await r.step(60)
    const res = ctrlOps(r.ctrl, 'result').find((m) => m.id === 'x')!
    expect([res.status, res.code, res.final]).toEqual(['rejected', 213, true])
  })
})

describe('ClockSync', () => {
  it('keeps the minimum-RTT sample, jumps beyond 50 ms and smooths below', () => {
    const c = new ClockSync()
    c.onPong(0, 10, 105) // rtt 10, off 100
    expect(c.offsetMs).toBe(100)
    c.onPong(20, 60, 200) // rtt 40, off 160: the min-RTT sample (off 100) still wins
    expect(c.offsetMs).toBe(100)
    c.onPong(100, 102, 221) // rtt 2, off 120: within 50 ms -> smoothed 0.1
    expect(c.offsetMs).toBeCloseTo(102, 6)
    c.onPong(200, 201, 500.5) // rtt 1, off 300: jump
    expect(c.offsetMs).toBe(300)
    expect(c.offsetMain(50, 10)).toBe(340)
  })
})

describe('transport', () => {
  it('backoff grows 1.5x from 500 ms, caps at 10 s, +-20 % jitter', () => {
    expect(backoffDelay(1, 0.5)).toBe(500)
    expect(backoffDelay(2, 0.5)).toBe(750)
    expect(backoffDelay(30, 0.5)).toBe(10_000)
    expect(backoffDelay(1, 0)).toBe(400)
    expect(backoffDelay(1, 1)).toBe(600)
    expect(protocolsFor('abc')).toEqual(['awr.rt.v1', 'bearer.abc'])
    expect(protocolsFor('')).toEqual(['awr.rt.v1'])
  })
  it('?source=fake maps to a fake: URL; other searches keep the WebSocket URL', () => {
    expect(resolveRtUrl('ws://h/api/rt', '?chrome=0')).toBe('ws://h/api/rt')
    const u = resolveRtUrl('ws://h/api/rt', '?source=fake&fakeN=200&fakeStart=1,2,3')
    expect(u.startsWith('fake:?')).toBe(true)
    const o = parseFakeUrl(u)
    expect(o.n).toBe(200)
    expect(o.start).toEqual([1, 2, 3])
  })
  it('contracts expose the layout id used by the fixtures', () => {
    expect(L.LAYOUT_ID).toBe(0x3d5e08d0)
  })
})
