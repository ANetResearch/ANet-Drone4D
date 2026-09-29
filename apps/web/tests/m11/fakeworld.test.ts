// Reconnect semantics end to end against the fake gateway (M11-FR-093, M11-AC-047 client part; AWR-17 §6.12, §6.13,
// §7.5; AWR-14 §7.7 reconciliation) and the FakeSource features used without a backend (M11-FR-098, M11-AC-040):
// resume with lastEventSeq and seq continuity, a call in flight re-sent with the same id answered `duplicate` and then
// finished, api restart (new sessionId: no resume, channels re-advertised), event gap on resume, fleet/cmd batches,
// ground start with takeoff, velocity teleoperation over CLIENT_DATA with the 250 ms watchdog, playback 213, periodic
// drops, and the flight60 `scene=full&source=fake` URL mapping.
import { describe, expect, it } from 'vitest'
import { FlightState } from '@awr/contracts/enums'
import { resolveRtUrl } from '@/net/rt/client'
import { batchCountsOf, batchSummaryOf } from '@/net/rt/batch'
import type { CallResult } from '@/net/rt/types'
import { ctrlOps, frames, rig } from '../net/harness'

const eventSeqs = (ctrl: { op: string }[]): number[] => {
  const out: number[] = []
  for (const m of ctrl as Record<string, unknown>[]) {
    if (m.op === 'event') out.push(Number(m.seq))
    else if (m.op === 'events') for (const it of m.items as { seq: number }[]) out.push(it.seq)
  }
  return out
}
const results = (ctrl: { op: string }[], id: string): CallResult[] =>
  (ctrl as Record<string, unknown>[]).filter((m) => m.op === 'result' && m.id === id) as unknown as CallResult[]

describe('reconnect to the same gateway', () => {
  it('resume: events continue without a gap; the in-flight goto is re-sent, answered duplicate, then succeeds', async () => {
    const r = await rig({ url: 'fake:?n=1', start: [0, 0, 50], eventPeriodS: 0.2 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'event', rate: 0, mode: 'all' }, { id: 2, topic: 'swarm/state', rate: 10, mode: 'latest' }] })
    await frames(r, 30)
    r.host.onMessage({ cmd: 'call', id: 'g-1', service: 'uav/p600-01/cmd/goto', args: { pos: [40, 0, 50], speed_mps: 8 }, timeoutMs: 3000 })
    await frames(r, 30)
    expect(results(r.ctrl, 'g-1').map((x) => x.status)).toEqual(['accepted', 'running'])
    const seqBefore = eventSeqs(r.ctrl)
    const lastSeq = Math.max(...seqBefore)
    // the link drops; the fake keeps simulating and emitting while we are away
    r.fake.serverClose(1006)
    for (let k = 0; k < 20; k++) {
      r.world!.tick()
      r.clock.t += 50
    }
    await frames(r, 60)
    expect(r.host.connState).toBe('LIVE')
    const hello = r.fake.received.find((m) => m.op === 'hello')!
    expect(hello.resume).toEqual({ sessionId: r.world!.sessionId, lastEventSeq: lastSeq })
    const seqs = eventSeqs(r.ctrl)
    const after = seqs.filter((s) => s > lastSeq)
    expect(after.length).toBeGreaterThan(3)
    // continuity: every seq after the drop arrives exactly once
    const sorted = [...new Set(after)].sort((a, b) => a - b)
    expect(sorted[0]).toBe(lastSeq + 1)
    for (let i = 1; i < sorted.length; i++) expect(sorted[i]).toBe(sorted[i - 1] + 1)
    expect(after.length).toBe(sorted.length)
    const re = r.fake.received.filter((m) => m.op === 'call' && m.id === 'g-1')
    expect(re).toHaveLength(1) // re-sent on the new connection with the same id
    expect(results(r.ctrl, 'g-1').some((x) => x.duplicate === true)).toBe(true)
    await frames(r, 60 * 8)
    const fin = results(r.ctrl, 'g-1').at(-1)!
    expect([fin.status, fin.final]).toEqual(['succeeded', true])
    expect(r.world!.stats.duplicates).toBe(1)
  })

  it('api restart: new sessionId, no resume, channels re-advertised, subscriptions replayed', async () => {
    const r = await rig({ url: 'fake:?n=3', n: 3 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 10, mode: 'latest' }, { id: 2, topic: 'event', rate: 0, mode: 'all' }] })
    await frames(r, 150)
    r.world!.restart()
    await frames(r, 60)
    expect(r.host.connState).toBe('LIVE')
    const connected = ctrlOps(r.ctrl, 'connected')
    expect(connected.map((m) => m.sameSession)).toEqual([false, false])
    const hellos = r.fake.received.filter((m) => m.op === 'hello')
    expect(hellos[0].resume).toBeUndefined()
    const subs = r.fake.received.filter((m) => m.op === 'subscribe')
    expect(subs.length).toBe(1)
    expect((subs[0].subs as unknown[]).length).toBe(2)
    expect(r.slots.at(-1)!.hdr.swarmN).toBe(3)
  })

  it('resume after the ring moved on: BATCH GAP and status events.gap', async () => {
    const r = await rig({ url: 'fake:?n=1', eventPeriodS: 3600 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'event', rate: 0, mode: 'all' }, { id: 2, topic: 'swarm/state', rate: 10, mode: 'latest' }] })
    await frames(r, 10)
    r.world!.emitStorm(3)
    await frames(r, 10)
    r.fake.serverClose(1001)
    r.world!.emitStorm(5000) // beyond the 4096 ring
    await frames(r, 80)
    expect(r.host.connState).toBe('LIVE')
    expect(ctrlOps(r.ctrl, 'status').some((m) => m.id === 'events.gap')).toBe(true)
    expect(r.host.counters.eventGaps).toBeGreaterThanOrEqual(1)
  })
})

describe('fleet/cmd batches', () => {
  it('hover on all: summary, counts, fleet.batch.progress, final succeeded', async () => {
    const r = await rig({ url: 'fake:?n=4', n: 4 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'event', rate: 0, mode: 'all' }] })
    await frames(r, 5)
    r.host.onMessage({ cmd: 'call', id: 'b-1', service: 'fleet/cmd/hover', args: { vehicles: '*' }, timeoutMs: 3000 })
    await frames(r, 30)
    const rs = results(r.ctrl, 'b-1')
    const sum = batchSummaryOf(rs[0])!
    expect(sum).toMatchObject({ accepted_n: 4, rejected_n: 0 })
    expect(rs.at(-1)).toMatchObject({ status: 'succeeded', final: true })
    expect(batchCountsOf(rs.at(-1)!)).toMatchObject({ succeeded: 4, running: 0, accepted: 0 })
    const ev = (ctrlOps(r.ctrl, 'events').flatMap((m) => m.items as Record<string, unknown>[]).concat(ctrlOps(r.ctrl, 'event') as Record<string, unknown>[]))
      .filter((e) => e.type === 'fleet.batch.progress')
    expect(ev.length).toBeGreaterThan(0)
    // children are aggregated: no per-vehicle cmd.* events for b-1:*
    expect(ctrlOps(r.ctrl, 'events').flatMap((m) => m.items as Record<string, unknown>[]).some((e) => String(e.cid).startsWith('b-1:'))).toBe(false)
  })

  it('rtl on a list with an unknown id: rejected_by_code, progress counts, vehicles land at home', async () => {
    const r = await rig({ url: 'fake:?n=3', n: 3 })
    await frames(r, 5)
    r.host.onMessage({ cmd: 'call', id: 'b-2', service: 'fleet/cmd/rtl', args: { vehicles: ['uav0001', 'uav0002', 'nope'] }, timeoutMs: 3000 })
    await frames(r, 60 * 40)
    const rs = results(r.ctrl, 'b-2')
    const sum = batchSummaryOf(rs[0])!
    expect(sum.accepted_n).toBe(2)
    expect(sum.rejected).toEqual([['nope', 107]])
    expect(sum.rejected_by_code).toEqual({ 107: 1 })
    const prog = ctrlOps(r.ctrl, 'progress').filter((m) => m.id === 'b-2').map((m) => batchCountsOf(m as never)!)
    expect(prog.some((c) => c.running > 0)).toBe(true)
    expect(rs.at(-1)).toMatchObject({ status: 'succeeded', final: true })
    expect(r.world!.fs[0] & 0x1f).toBe(FlightState.LANDED)
    expect(r.world!.fs[1] & 0x1f).toBe(FlightState.LANDED)
    expect(r.world!.fs[2] & 0x1f).toBe(FlightState.FLYING)
    expect(Math.hypot(...r.world!.positionOf(0))).toBeLessThan(1.5)
  })

  it('an op outside the batch list is rejected 110', async () => {
    const r = await rig({ url: 'fake:?n=2', n: 2 })
    await frames(r, 5)
    r.host.onMessage({ cmd: 'call', id: 'b-3', service: 'fleet/cmd/goto', args: { vehicles: '*' }, timeoutMs: 3000 })
    await frames(r, 5)
    expect(results(r.ctrl, 'b-3')[0]).toMatchObject({ status: 'rejected', code: 110, final: true })
  })
})

describe('single-vehicle commands', () => {
  it('ground start: goto is refused (105) until takeoff succeeds', async () => {
    const r = await rig({ url: 'fake:?n=1&ground=1', ground: true })
    await frames(r, 5)
    r.host.onMessage({ cmd: 'call', id: 's-1', service: 'uav/p600-01/cmd/goto', args: { pos: [10, 0, 10] }, timeoutMs: 3000 })
    await frames(r, 5)
    expect(results(r.ctrl, 's-1')[0]).toMatchObject({ status: 'rejected', code: 105 })
    r.host.onMessage({ cmd: 'call', id: 's-2', service: 'uav/p600-01/cmd/takeoff', args: { alt_m: 5 }, timeoutMs: 3000 })
    await frames(r, 60 * 6)
    expect(results(r.ctrl, 's-2').map((x) => x.status)).toEqual(['accepted', 'running', 'succeeded'])
    expect(r.world!.positionOf(0)[2]).toBeCloseTo(5, 0)
    r.host.onMessage({ cmd: 'call', id: 's-3', service: 'uav/p600-01/cmd/goto', args: { pos: [10, 0, 10], speed_mps: 10 }, timeoutMs: 3000 })
    await frames(r, 60 * 4)
    expect(results(r.ctrl, 's-3').at(-1)).toMatchObject({ status: 'succeeded' })
    r.host.onMessage({ cmd: 'call', id: 's-4', service: 'uav/p600-01/cmd/land', args: {}, timeoutMs: 3000 })
    await frames(r, 60 * 10)
    expect(results(r.ctrl, 's-4').at(-1)).toMatchObject({ status: 'succeeded' })
    expect(r.world!.fs[0] & 0x1f).toBe(FlightState.LANDED)
  })

  it('velocity over CLIENT_DATA moves the vehicle; the 250 ms watchdog ends the call with 209', async () => {
    const r = await rig({ url: 'fake:?n=1', start: [0, 0, 30] })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'fleet/roster', rate: 10, mode: 'latest' }] })
    await frames(r, 10)
    // a setpoint without an active velocity call: 322 once
    r.host.onMessage({ cmd: 'setpoint', agentNo: 0, id: 'p600-01', vx: 1, vy: 0, vz: 0, yawRate: 0, final: false })
    await frames(r, 2)
    expect(ctrlOps(r.ctrl, 'error').some((m) => m.code === 322)).toBe(true)
    r.host.onMessage({ cmd: 'call', id: 'v-1', service: 'uav/p600-01/cmd/velocity', args: { frame: 'world', vmax_mps: 5 }, timeoutMs: 3000 })
    await frames(r, 2)
    for (let k = 0; k < 60; k++) {
      if (k % 3 === 0) r.host.onMessage({ cmd: 'setpoint', agentNo: 0, id: 'p600-01', vx: 4, vy: 3, vz: 0, yawRate: 0, final: false }) // 20 Hz
      await frames(r, 1)
    }
    const p = r.world!.positionOf(0)
    expect(p[0]).toBeGreaterThan(2.5)
    expect(p[1]).toBeGreaterThan(1.8)
    expect(results(r.ctrl, 'v-1').map((x) => x.status)).toEqual(['accepted', 'running'])
    const adv = r.fake.received.filter((m) => m.op === 'advertise')
    expect(adv[0]).toMatchObject({ channels: [{ id: 1, topic: 'uav/p600-01/setpoint', encoding: 'raw', schemaName: 'awr.VelSetpoint16.v1' }] })
    expect(r.fake.stats.setpoints).toBeGreaterThan(15)
    await frames(r, 30) // silence > 250 ms
    expect(results(r.ctrl, 'v-1').at(-1)).toMatchObject({ status: 'canceled', code: 209, final: true })
  })

  it('cancel while running ends the call canceled (6)', async () => {
    const r = await rig({ url: 'fake:?n=1', start: [0, 0, 50] })
    await frames(r, 5)
    r.host.onMessage({ cmd: 'call', id: 'c-9', service: 'uav/p600-01/cmd/goto', args: { pos: [200, 0, 50], speed_mps: 5 }, timeoutMs: 3000 })
    await frames(r, 20)
    r.host.onMessage({ cmd: 'cancel', id: 'c-9' })
    await frames(r, 5)
    expect(results(r.ctrl, 'c-9').at(-1)).toMatchObject({ status: 'canceled', code: 6, final: true })
  })
})

describe('other fake gateway behaviour', () => {
  it('playback is unavailable in D1-core (213)', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 3)
    r.host.onMessage({ cmd: 'playback', msg: { cmd: 'open', run: 'r1', segment: 0, request_id: 'pb-1' } })
    await frames(r, 3)
    expect(ctrlOps(r.ctrl, 'playbackState')[0]).toMatchObject({ status: 'error', code: 213, request_id: 'pb-1' })
  })

  it('drop every s closes the link with 1001 and the client comes back', async () => {
    const r = await rig({ url: 'fake:?n=1&drop=1', dropEveryS: 1 })
    await frames(r, 200)
    expect(r.sockets).toBeGreaterThanOrEqual(3)
    expect(r.host.counters.reconnects).toBeGreaterThanOrEqual(2)
    expect(ctrlOps(r.ctrl, 'conn').filter((m) => m.state === 'RECONNECTING' && m.code === 1001).length).toBeGreaterThanOrEqual(2)
  })

  it('status banners are re-sent on every connection', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 3)
    r.world!.setStatus('proc.sim-core', 'warning', 'sim restarting')
    await frames(r, 3)
    r.fake.serverClose(1011)
    await frames(r, 60)
    expect(ctrlOps(r.ctrl, 'status').filter((m) => m.id === 'proc.sim-core')).toHaveLength(2)
  })
})

describe('flight60 source=fake URL (AWR-18 §8.6)', () => {
  it('scene=full uses the 2 vehicles of S1, n overrides, fakeN wins', () => {
    expect(resolveRtUrl('ws://x/api/rt', '?bench=flight60&scene=full&source=fake')).toBe('fake:?n=2')
    expect(resolveRtUrl('ws://x/api/rt', '?bench=flight60&scene=full&source=fake&n=200')).toBe('fake:?n=200')
    expect(resolveRtUrl('ws://x/api/rt', '?bench=flight60&scene=full&source=fake&n=200&fakeN=5')).toBe('fake:?n=5')
    expect(resolveRtUrl('ws://x/api/rt', '?bench=flight60&scene=full&source=live')).toBe('ws://x/api/rt')
    expect(resolveRtUrl('ws://x/api/rt', '?source=fake&fakeN=3&fakeGround=1&fakeDrop=30')).toBe('fake:?n=3&ground=1&drop=30')
  })
})
