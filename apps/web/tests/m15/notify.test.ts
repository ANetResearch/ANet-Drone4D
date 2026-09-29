// M15-FR-025, FR-036, FR-088, FR-089 (AWR-14 §11; D1-AC-27): event severity and merge keys, the 5000-entry event log,
// alarm merging (500 same-key events -> one alarm, one toast), acknowledgement, the bridge flush with injected events,
// vehicle state alarms that clear when the vehicle leaves the state, and the one-red evaluation of the DOM figures.
import { describe, expect, it, vi } from 'vitest'
import type { RtEvent } from '@/net/rt'

// the first import of the viewport facade and three.js is slow on a loaded machine
vi.setConfig({ testTimeout: 30_000 })

let seq = 0
const ev = (type: string, level: 0 | 1 | 2 | 3, data: Record<string, unknown> = {}, uav: string | null = null): RtEvent =>
  ({ seq: ++seq, t_sim_ns: seq * 1e6, type, level, uav, cid: null, data })

const FORBIDDEN = /[\p{Extended_Pictographic}\p{Emoji_Presentation}\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]|\u{FE0F}/u

describe('severity', () => {
  it('maps wire levels and flight states (AWR-14 §11.1)', async () => {
    const { eventSeverity, mergeKeyOf, flightStateOf } = await import('@/ui/notify/severity')
    expect(eventSeverity(ev('sim.started', 0)).sev).toBe('info')
    expect(eventSeverity(ev('env.warning', 2)).sev).toBe('warning')
    expect(eventSeverity(ev('proc.state', 3)).sev).toBe('critical')
    expect(eventSeverity(ev('uav.state', 1, { to: 'HOLD/LINK_LOSS' })).sev).toBe('warning')
    const crit = eventSeverity(ev('uav.state', 1, { to: 'CRASHED/IMPACT' }))
    expect(crit.sev).toBe('critical')
    expect(crit.rank).toBe(8)
    expect(eventSeverity(ev('uav.state', 0, { to: 'FLYING/GOTO' })).sev).toBe('info')
    expect(eventSeverity(ev('cmd.rejected', 1, { code: 102 })).sev).toBe('warning')
    expect(flightStateOf('ELAND/DESCENT')).toBe(10)
    expect(mergeKeyOf(ev('uav.state', 2, { to: 'HOLD/LINK_LOSS' }))).toBe('uav:uav.state:LINK_LOSS')
    expect(mergeKeyOf(ev('cmd.rejected', 1, { code: 102 }))).toBe('cmd:cmd.rejected:102')
  })

  it('describes events without forbidden glyphs, sanitising wire text', async () => {
    const { describeEvent } = await import('@/ui/notify/severity')
    const s = describeEvent(ev('env.warning', 2, { message: 'wind \u{1F32A}\u{FE0F} limit \u{2605}' }, 'p600-01'))
    expect(s).toContain('p600-01')
    expect(FORBIDDEN.test(s)).toBe(false)
    expect(describeEvent(ev('uav.state', 2, { to: 'HOLD/LINK_LOSS' }, 'p600-02'))).toContain('p600-02')
    expect(describeEvent(ev('cmd.rejected', 1, { op: 'goto', code: 102 }, 'p600-03'))).toContain('102')
  })
})

describe('event log', () => {
  it('keeps the newest 5000 of 6000 events (M15-AC-018)', async () => {
    const { EventRing } = await import('@/ui/notify/eventLog')
    const { LIMITS } = await import('@/lib/tokens/input.gen')
    const ring = new EventRing(LIMITS.eventLogCap)
    const first = seq + 1
    for (let i = 0; i < 6000; i++) ring.push(ev('sim.clock', 0), i)
    expect(ring.len).toBe(5000)
    expect(ring.dropped).toBe(1000)
    expect(ring.get(ring.slotOfNewest(0))?.seq).toBe(first + 5999)
    expect(ring.get(ring.slotOfNewest(4999))?.seq).toBe(first + 1000)
  })
})

describe('alarms and toasts', () => {
  it('merges 500 same-key events into one alarm and one toast (M15-AC-047)', async () => {
    const { alarms, alarmsStore } = await import('@/ui/notify/alarms')
    const { pushEvents, flushEvents } = await import('@/ui/notify/eventBridge')
    const { UX } = await import('@/ui/testing/uxProbe')
    alarms.clear()
    const batch: RtEvent[] = []
    for (let i = 0; i < 500; i++) batch.push(ev('safety.link', 2, { reason: 'link_drop' }, `uav${String(i).padStart(4, '0')}`))
    const toastsBefore = { ...UX.toasts.merged }
    pushEvents(batch)
    flushEvents()
    const list = alarms.list()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ count: 500, severity: 'warning' })
    expect(list[0].subjects).toHaveLength(500)
    const key = `alarm:${list[0].key}`
    expect((UX.toasts.merged[key] ?? 0) - (toastsBefore[key] ?? 0)).toBe(1)
    expect(alarmsStore.getState().warnActive).toBe(1)
  })

  it('keeps criticals until acknowledged and clears state alarms when the vehicle leaves the state', async () => {
    const { alarms, alarmsStore } = await import('@/ui/notify/alarms')
    const { pushEvents, flushEvents } = await import('@/ui/notify/eventBridge')
    alarms.clear()
    pushEvents([ev('uav.state', 3, { to: 'FAILSAFE/CONTROLLED' }, 'p600-07'), ev('uav.state', 2, { to: 'HOLD/LINK_LOSS' }, 'p600-08')])
    flushEvents()
    expect(alarmsStore.getState()).toMatchObject({ critUnacked: 1, warnActive: 1 })
    expect(alarms.criticalUnacked('p600-07')).not.toBeNull()
    // p600-08 recovers: its HOLD alarm is no longer active
    pushEvents([ev('uav.state', 0, { to: 'FLYING/HOVER' }, 'p600-08')])
    flushEvents()
    expect(alarmsStore.getState().warnActive).toBe(0)
    alarms.ackAll()
    expect(alarmsStore.getState().critUnacked).toBe(0)
    expect(alarms.criticalUnacked('p600-07')).toBeNull()
    // a new raise re-arms the acknowledgement
    pushEvents([ev('uav.state', 3, { to: 'FAILSAFE/CONTROLLED' }, 'p600-07')])
    flushEvents()
    expect(alarmsStore.getState().critUnacked).toBe(1)
  })

  it('writes the event log and the bridge statistics once per flush', async () => {
    const { pushEvents, flushEvents } = await import('@/ui/notify/eventBridge')
    const { eventLogStore } = await import('@/ui/notify/eventLog')
    const { UX } = await import('@/ui/testing/uxProbe')
    const v0 = eventLogStore.getState().version
    const f0 = UX.bridge.flushes
    pushEvents([ev('sim.clock', 0), ev('sim.clock', 0), ev('sim.clock', 0)])
    flushEvents()
    flushEvents() // empty: no write
    expect(eventLogStore.getState().version).toBe(v0 + 1)
    expect(UX.bridge.flushes).toBe(f0 + 1)
    expect(UX.bridge.dropped).toBe(0)
  })

  it('formats merged subjects as "a, b, c and N more"', async () => {
    const { subjectsText } = await import('@/ui/notify/toastMerger')
    const s = subjectsText(['a', 'b', 'c', 'd', 'e'])
    expect(s).toContain('a')
    expect(s).toContain('2')
    expect(subjectsText(['a'])).toBe('a')
  })
})

describe('query invalidation from events', () => {
  it('maps event types to query keys (M15-FR-039)', async () => {
    const { planInvalidation } = await import('@/app/query/eventInvalidation')
    const p = planInvalidation([ev('sim.vehicle.state', 1, { to: 'READY' }), ev('mission.state', 0), ev('session.switched', 0, { world_id: 'suzhou' })])
    expect(p).toMatchObject({ fleet: true, missions: true, session: true, switchedTo: 'suzhou', worlds: false })
    expect(planInvalidation([ev('env.changed', 0)])).toMatchObject({ fleet: false, missions: false, worlds: false })
  })
})
