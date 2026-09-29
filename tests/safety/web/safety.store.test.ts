// M09 stores/safety.ts（M09 §8.1–§8.2；FR-102、FR-103）：级别映射、告警开闭、同合并键 Toast 合并（1000 架同码 -> 1 条）、
// safety 行派生字段、写入合批（≤ 4 次/s）与 bindSafety 订阅随主选切换。
import { beforeEach, describe, expect, it } from 'vitest'
import type { DataMsg, RtEvent } from '@/net/rt'
import {
  ackAllAlarms, alarmFromEvent, bindSafety, flushSafety, ingestSafetyEvents, resetSafety, safetyStore, setSafetyRow,
  severityOfLevel, TOAST_MAX, type SafetyRow,
} from '@/stores/safety'

function ev(uav: string, code: string, type: string, level: 0 | 1 | 2 | 3, data: Record<string, unknown> = {}): RtEvent {
  return { seq: 1, t_sim_ns: 1_000_000, type, level, uav, data: { code, rank: 2, ...data } }
}

beforeEach(() => resetSafety())

describe('safety store', () => {
  it('maps wire levels', () => {
    expect([0, 1, 2, 3].map(severityOfLevel)).toEqual(['info', 'info', 'warning', 'critical'])
    const a = alarmFromEvent(ev('u1', 'SAF.CTRL.POS_ERR_ELAND', 'safety.fastguard', 3, { to: { state: 'ELAND', sub: 'CONTROLLED' } }), 10)!
    expect(a.severity).toBe('critical')
    expect(a.mergeKey).toBe('safety:eland')
    expect(a.to).toBe('ELAND/CONTROLLED')
    expect(alarmFromEvent({ seq: 2, t_sim_ns: 0, type: 'uav.state', level: 2, data: {} }, 0)).toBeNull()
  })

  it('opens, merges and closes alarms; toasts merge by key', () => {
    const batch: RtEvent[] = []
    for (let i = 0; i < 1000; i++) batch.push(ev(`u${i}`, 'SAF.LINK.LOST_HOLD', 'safety.link', 2))
    expect(ingestSafetyEvents(batch, 100)).toBe(1000)
    ingestSafetyEvents([ev('u7', 'SAF.EST.TIMEOUT', 'safety.fastguard', 3, { rank: 6 })], 110)
    expect(flushSafety(1000)).toBe(true)
    const s = safetyStore.getState()
    expect(s.toasts.length).toBeLessThanOrEqual(TOAST_MAX)
    const link = s.toasts.find((t) => t.key === 'safety:hold:link_loss')!
    expect(link.vehicles.length).toBe(1000)
    expect(s.unackedCritical).toBe(1)
    expect(s.alarms[0].code).toBe('SAF.EST.TIMEOUT')
    // RESTORED closes the open alarm of the same type for that vehicle
    ingestSafetyEvents([ev('u903', 'SAF.LINK.RESTORED', 'safety.link', 1)], 2000)
    flushSafety(3000)
    const u3 = safetyStore.getState().alarms.find((a) => a.key === 'u903|SAF.LINK.LOST_HOLD')!
    expect(u3.active).toBe(false)
    ackAllAlarms(3100)
    flushSafety(3400)
    expect(safetyStore.getState().unackedCritical).toBe(0)
  })

  it('derives selected-row fields and coalesces writes', () => {
    const row: SafetyRow = {
      active: [{ code: 'SAF.GEOFENCE.NEAR', level: 2, since_t_ns: 1 }, { code: 'SAF.BAT.EMERG', level: 3, since_t_ns: 2 }],
      fsm: { state: 'RTL', sub: 'CRUISE', latched: [], auto: true },
      geofence_margin_m: 3.2, separation_m: null, battery_rtl: { t_rem_s: 390, t_rtl_s: 100 },
      energy: { soc_pct: 41.5, soc_rtl_pct: 12.0, z_rtl_m: 45 }, resume: { ok: false, blocked_by: 'BATTERY' },
    }
    const v0 = safetyStore.getState().version
    setSafetyRow('u1', row, 0)
    setSafetyRow('u1', row, 10)
    expect(flushSafety(300)).toBe(true)
    expect(safetyStore.getState().version).toBe(v0 + 1)
    const sel = safetyStore.getState().selected!
    expect(sel.guard).toBe('SAF.BAT.EMERG')
    expect(sel.geofence).toBe('near')
    expect(sel.energyMargin).toBeCloseTo(3.0)
    expect(sel.rtlReservePct).toBe(12)
    expect(sel.resumeOk).toBe(false)
    expect(sel.resumeBlockedBy).toBe('BATTERY')
    expect(flushSafety(400)).toBe(false)
    expect(flushSafety(300 + 3500)).toBe(true)
    expect(safetyStore.getState().selected!.stale).toBe(true)
  })

  it('binds events and follows the primary selection', () => {
    const subs: string[] = []
    const released: string[] = []
    let evCb: ((b: readonly RtEvent[]) => void) | null = null
    let dataCb: ((m: DataMsg) => void) | null = null
    let selCb: ((s: { primary: string | null }) => void) | null = null
    let primary: string | null = 'u1'
    const rt = {
      onEvents: (cb: (b: readonly RtEvent[]) => void) => { evCb = cb; return () => { evCb = null } },
      onData: (cb: (m: DataMsg) => void) => { dataCb = cb; return () => { dataCb = null } },
      subscribe: (t: string) => { subs.push(t); return () => released.push(t) },
    }
    const sel = { getState: () => ({ primary }), subscribe: (cb: (s: { primary: string | null }) => void) => { selCb = cb; return () => { selCb = null } } }
    let now = 0
    const off = bindSafety(rt, sel, () => now)
    expect(subs).toEqual(['uav/u1/safety'])
    now = 50
    dataCb!({ topic: 'uav/u1/safety', channelId: 100, seq: 1, tSimMs: 1, data: { active: [], fsm: { state: 'HOLD', sub: 'LINK_LOSS', latched: [] }, geofence_margin_m: 20, separation_m: 8, battery_rtl: null } })
    evCb!([ev('u1', 'SAF.LINK.LOST_HOLD', 'safety.link', 2)])
    flushSafety(400)
    expect(safetyStore.getState().selected!.flightState).toBe('HOLD')
    expect(safetyStore.getState().alarms.length).toBe(1)
    primary = 'u2'
    selCb!({ primary })
    expect(released).toEqual(['uav/u1/safety'])
    expect(subs).toEqual(['uav/u1/safety', 'uav/u2/safety'])
    off()
    expect(released).toEqual(['uav/u1/safety', 'uav/u2/safety'])
    expect(evCb).toBeNull()
  })
})
