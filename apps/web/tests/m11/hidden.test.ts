// Hidden page (M11-FR-095, M11-AC-036; M11 §6.4.14 point 3): while the main thread does not pull, the worker keeps
// pinging, delivers non-event control messages (results, conn) on a slow timer and keeps at most 8192 events, dropping
// the oldest with a local GAP; once visible the kept events arrive after a localGap notice with the dropped seq range,
// and the next slot carries the GAP flag.
import { describe, expect, it } from 'vitest'
import { EVENT_BUFFER_CAP } from '@/net/rt/session'
import { SF } from '@/net/rt/frame'
import { ctrlOps, rig } from '../net/harness'

describe('hidden page', () => {
  it('60 s at 200 events/s: at most 8192 events kept, local GAP, results still delivered', async () => {
    const r = await rig({ url: 'fake:?n=4', n: 4, eventPeriodS: 3600 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'event', rate: 0, mode: 'all' }, { id: 2, topic: 'swarm/state', rate: 10, mode: 'latest' }] })
    await r.step(20)
    r.pull()
    await r.step(20)
    r.pull()
    const deliveredBefore = r.ctrl.length
    r.host.onMessage({ cmd: 'visibility', hidden: true })
    const pingsBefore = r.fake.stats.pings
    let emitted = 0
    for (let k = 0; k < 600; k++) {
      r.world!.emitStorm(20) // 200 events/s
      emitted += 20
      await r.step(100)
      if (k === 300) r.host.onMessage({ cmd: 'call', id: 'h-1', service: 'uav/uav0001/cmd/hover', args: {}, timeoutMs: 3000 })
    }
    const whileHidden = r.ctrl.slice(deliveredBefore)
    expect(whileHidden.some((m) => m.op === 'event' || m.op === 'events')).toBe(false)
    expect(whileHidden.some((m) => m.op === 'result' && m.id === 'h-1' && m.status === 'succeeded')).toBe(true)
    expect(r.fake.stats.pings - pingsBefore).toBeGreaterThanOrEqual(100) // 2 Hz for 60 s
    // visible again: the kept events, preceded by the gap notice
    r.host.onMessage({ cmd: 'visibility', hidden: false })
    await r.step(100)
    const after = r.ctrl.slice(deliveredBefore + whileHidden.length)
    const gap = after.find((m) => m.op === 'localGap')!
    expect(gap).toBeDefined()
    let kept = 0
    let firstKept = Number.POSITIVE_INFINITY
    for (const m of after) {
      if (m.op === 'event') {
        kept++
        firstKept = Math.min(firstKept, Number(m.seq))
      } else if (m.op === 'events') {
        const items = m.items as { seq: number }[]
        kept += items.length
        for (const it of items) firstKept = Math.min(firstKept, it.seq)
      }
    }
    expect(kept).toBeLessThanOrEqual(EVENT_BUFFER_CAP)
    expect(kept + Number(gap.dropped)).toBeGreaterThanOrEqual(emitted)
    expect(Number(gap.toSeq)).toBeLessThan(firstKept)
    expect(Number(gap.fromSeq)).toBeLessThanOrEqual(Number(gap.toSeq))
    expect(r.host.counters.eventGaps).toBeGreaterThanOrEqual(1)
    r.pull()
    await r.step(20)
    r.pull()
    expect(r.slots.slice(-3).some((s) => (s.hdr.flags & SF.GAP) !== 0 && s.hdr.eventGaps >= 1)).toBe(true)
  })

  it('visible page without pulls (no frame loop) still gets events on the 50 ms path', async () => {
    const r = await rig({ url: 'fake:?n=2', n: 2, eventPeriodS: 3600 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'event', rate: 0, mode: 'all' }] })
    await r.step(20)
    r.world!.emitStorm(5)
    for (let k = 0; k < 5; k++) await r.step(20)
    const got = ctrlOps(r.ctrl, 'events').flatMap((m) => m.items as unknown[]).length + ctrlOps(r.ctrl, 'event').length
    expect(got).toBe(5)
  })

  it('without any pull the roster and msgpack channels still arrive on the control path', async () => {
    const r = await rig({ url: 'fake:?n=3', n: 3 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'fleet/roster', rate: 10, mode: 'latest' }, { id: 2, topic: 'env/state', rate: 10, mode: 'latest' },
      { id: 3, topic: 'swarm/state', rate: 10, mode: 'latest' }] })
    for (let k = 0; k < 20; k++) await r.step(20)
    expect(r.slots).toHaveLength(0) // nobody pulled
    expect(ctrlOps(r.ctrl, 'roster')[0].entries).toHaveLength(3)
    expect(ctrlOps(r.ctrl, 'data').some((m) => m.topic === 'env/state')).toBe(true)
    // the raw swarm reference is still waiting for the first pull
    r.pull()
    expect(r.slots.at(-1)!.hdr.swarmN).toBe(3)
  })
})

