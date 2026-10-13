// M15-FR-219 (AWR-04 §10.7; AWR-14 §20.11; UX-FR-138, UX-FR-139; M15-AC-213 unit part): captures toast once per target
// per 30 s, a discovery is a per-target critical alarm (rank 5) whose toast confirm acknowledges it and leaves the red
// candidates, the D2 event texts and the four filters of the events tab, and the event-sourced target live state.
import { describe, expect, it, vi } from 'vitest'
import type { RtEvent } from '@/net/rt'

vi.setConfig({ testTimeout: 30_000 })

let seq = 0
const ev = (type: string, level: 0 | 1 | 2 | 3, data: Record<string, unknown>, uav: string | null = null): RtEvent =>
  ({ seq: ++seq, t_sim_ns: seq * 1e6, type, level, uav, data })

describe('notices', () => {
  it('toasts a capture once per target per 30 s (wall clock)', async () => {
    const n = await import('@/ui/notify/d2Notices')
    n.resetNoticeDedupe()
    expect(n.dueOnce('capture:person-01', n.CAPTURE_DEDUPE_MS, 1000)).toBe(true)
    expect(n.dueOnce('capture:person-01', n.CAPTURE_DEDUPE_MS, 20_000)).toBe(false)
    expect(n.dueOnce('capture:dog-01', n.CAPTURE_DEDUPE_MS, 20_000)).toBe(true)
    expect(n.dueOnce('capture:person-01', n.CAPTURE_DEDUPE_MS, 31_001)).toBe(true)
  })

  it('a discovery is a critical alarm per target with rank 5; confirming acknowledges it', async () => {
    const { consumeAlarm, alarms } = await import('@/ui/notify/alarms')
    const { eventSeverity, mergeKeyOf } = await import('@/ui/notify/severity')
    const { noticeOf } = await import('@/ui/notify/d2Notices')
    const { confirmDiscovery, discoveryKey, unconfirmedDiscoveries } = await import('@/ui/notify/discoveries')
    const e1 = ev('target.discovered', 3, { target: 'person-01', uav: 'p600-02', kind: 'visual', range_m: 148 }, 'p600-02')
    const e2 = ev('target.discovered', 3, { target: 'dog-01', uav: 'p600-03', kind: 'acoustic', range_m: 90 }, 'p600-03')
    expect(eventSeverity(e1)).toEqual({ sev: 'critical', rank: 5 })
    expect(mergeKeyOf(e1)).toBe(discoveryKey('person-01'))
    consumeAlarm(e1, 1000)
    consumeAlarm(e2, 2000)
    noticeOf(e1, 1000)
    noticeOf(e2, 2000)
    expect([...unconfirmedDiscoveries().keys()].sort()).toEqual(['dog-01', 'person-01'])
    confirmDiscovery('person-01')
    expect(alarms.get(discoveryKey('person-01'))?.acked).toBe(true)
    expect([...unconfirmedDiscoveries().keys()]).toEqual(['dog-01'])
    // acknowledged in the alarm centre: gone from the candidates as well
    alarms.ack(discoveryKey('dog-01'))
    expect(unconfirmedDiscoveries().size).toBe(0)
  })

  it('describes the D2 events and sorts them into the four filters', async () => {
    const { describeD2, d2FilterOf } = await import('@/ui/notify/severity')
    expect(describeD2(ev('perception.capture', 1, { target: 'person-01', uav: 'uav0001', sensor: 'eo_gx40', level_to: 'R' }))).toBe('已明确捕获 person-01 · uav0001 eo_gx40 · 识别 R')
    expect(describeD2(ev('target.discovered', 3, { target: 'person-01', uav: 'p600-02', kind: 'visual', range_m: 148 }))).toBe('person-01 发现了 p600-02（视觉，148 m）')
    expect(describeD2(ev('uav.state', 1, {}))).toBeNull()
    expect(describeD2(ev('sensor.mode', 1, { uav: 'uav0001', sensor: 'eo_gx40', from: 'day', to: 'nir', reason: 'auto_low_light' }))).toBe('uav0001 eo_gx40 由日视切换为近红外（auto_low_light）')
    expect(describeD2(ev('sensor.mode', 1, { uav: 'uav0001', sensor: 'mid360', from: 'active', to: 'paused' }))).toBe('uav0001 mid360 由工作切换为暂停')
    expect(d2FilterOf('perception.capture')).toBe('perception')
    expect(d2FilterOf('target.state')).toBe('targets')
    expect(d2FilterOf('uav.keepout_guard')).toBe('targets')
    expect(d2FilterOf('relay.gap')).toBe('tasking')
    expect(d2FilterOf('nest.launch')).toBe('tasking')
    expect(d2FilterOf('sandbox.rate')).toBe('sandbox')
    expect(d2FilterOf('session.switched')).toBe('sandbox')
    expect(d2FilterOf('uav.state')).toBeNull()
  })

  it('the events tab filters by source and folds level changes', async () => {
    const { passesD2 } = await import('@/ui/panels/events/EventsPanel')
    expect(passesD2('uav.state', new Set(), false)).toBe(true)
    expect(passesD2('perception.level', new Set(), false)).toBe(false)
    expect(passesD2('perception.level', new Set(['perception']), true)).toBe(true)
    expect(passesD2('perception.capture', new Set(['targets']), false)).toBe(false)
    expect(passesD2('uav.state', new Set(['targets']), false)).toBe(false)
  })
})

