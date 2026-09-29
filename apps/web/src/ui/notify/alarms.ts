// Alarm store and merger (M15-FR-088, FR-033; AWR-14 §11.1, §11.4, §11.5): warning and critical events merge by the key
// source:type:reason into AlarmItems (subjects de-duplicated, the UI shows the first 3 + "and N more"); a vehicle
// state alarm stays active while the vehicle is still in that state; critical alarms persist until acknowledged or
// cleared. Acknowledgement is local to this client (never sent). Capacity LIMITS.alarmCap (512): the oldest inactive
// item goes first. The store is written once per bridge flush (<= 4 Hz) with {version, counts}.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { LIMITS } from '@/lib/tokens/input.gen'
import type { RtEvent } from '@/net/rt'
import { describeEvent, eventSeverity, mergeKeyOf, sourceOf, type Sev, type SevInfo } from './severity'

export interface AlarmItem {
  key: string
  severity: Exclude<Sev, 'info'>
  rank: number
  source: string
  type: string
  subjects: string[]
  /** subjects still in the alarm condition (vehicle states); empty for one-shot events */
  active: Set<string>
  count: number
  firstWallMs: number
  lastWallMs: number
  tSimNs: number
  text: string
  acked: boolean
  /** a one-shot event (command failure, env warning) stays listed but is not an ongoing condition */
  oneShot: boolean
}
export interface AlarmsState { version: number; critUnacked: number; warnActive: number; total: number }
export const alarmsStore = createAwrStore<AlarmsState>('ui.alarms', () => ({ version: 0, critUnacked: 0, warnActive: 0, total: 0 }))
export const useAlarms = <T,>(sel: (s: AlarmsState) => T): T => useStore(alarmsStore, sel)

const items = new Map<string, AlarmItem>()
/** vehicle id -> key of the state alarm it is currently in */
const vehicleKey = new Map<string, string>()
const si: SevInfo = { sev: 'info', rank: 0 }

export const isActive = (a: AlarmItem): boolean => (a.oneShot ? !a.acked : a.active.size > 0)

function evict(): void {
  if (items.size <= LIMITS.alarmCap) return
  let oldest: AlarmItem | null = null
  for (const a of items.values()) if (!isActive(a) && (!oldest || a.lastWallMs < oldest.lastWallMs)) oldest = a
  if (!oldest) for (const a of items.values()) if (!oldest || a.lastWallMs < oldest.lastWallMs) oldest = a
  if (oldest) items.delete(oldest.key)
}

function leave(uav: string): void {
  const k = vehicleKey.get(uav)
  if (k === undefined) return
  vehicleKey.delete(uav)
  const a = items.get(k)
  if (a) a.active.delete(uav)
}

/** fold one event into the alarms; returns the touched item (null for info events) */
export function consumeAlarm(e: RtEvent, wallMs: number): AlarmItem | null {
  const s = eventSeverity(e, si)
  const uav = e.uav ?? null
  const stateEvent = e.type === 'uav.state'
  if (stateEvent && uav) leave(uav)
  if (s.sev === 'info') return null
  const key = mergeKeyOf(e)
  let a = items.get(key)
  if (!a) {
    a = {
      key, severity: s.sev, rank: s.rank, source: sourceOf(e.type), type: e.type, subjects: [], active: new Set(), count: 0,
      firstWallMs: wallMs, lastWallMs: wallMs, tSimNs: e.t_sim_ns, text: '', acked: false, oneShot: !stateEvent,
    }
    items.set(key, a)
    evict()
  }
  a.count++
  a.lastWallMs = wallMs
  a.tSimNs = e.t_sim_ns
  a.text = describeEvent(e)
  if (s.rank > a.rank) a.rank = s.rank
  if (s.sev === 'critical') a.severity = 'critical'
  if (uav) {
    if (!a.subjects.includes(uav)) a.subjects.push(uav)
    if (stateEvent) {
      a.active.add(uav)
      vehicleKey.set(uav, key)
    }
  }
  // a new raise of a critical re-arms the acknowledgement
  if (s.sev === 'critical') a.acked = false
  return a
}

function counts(): Omit<AlarmsState, 'version'> {
  let critUnacked = 0
  let warnActive = 0
  for (const a of items.values()) {
    if (!isActive(a)) continue
    if (a.severity === 'critical' && !a.acked) critUnacked++
    else if (a.severity === 'warning') warnActive++
  }
  return { critUnacked, warnActive, total: items.size }
}

/** publish after a flush (one write) */
export function commitAlarms(): void {
  alarmsStore.setState({ ...counts(), version: alarmsStore.getState().version + 1 })
}

export const alarms = {
  /** sorted by severity, then activity, then the latest first */
  list(filter: 'all' | 'critical' | 'warning' = 'all'): AlarmItem[] {
    const out: AlarmItem[] = []
    for (const a of items.values()) if (filter === 'all' || a.severity === filter) out.push(a)
    return out.sort((x, y) => (y.severity === 'critical' ? 1 : 0) - (x.severity === 'critical' ? 1 : 0)
      || Number(isActive(y)) - Number(isActive(x)) || y.lastWallMs - x.lastWallMs)
  },
  ack(key: string): void {
    const a = items.get(key)
    if (!a || a.acked) return
    a.acked = true
    commitAlarms()
  },
  ackAll(): void {
    let any = false
    for (const a of items.values()) {
      if (!a.acked) {
        a.acked = true
        any = true
      }
    }
    if (any) commitAlarms()
  },
  /** the vehicle is in an unacknowledged critical state alarm (RedArbiter candidates) */
  criticalUnacked(uav: string): AlarmItem | null {
    const k = vehicleKey.get(uav)
    const a = k === undefined ? undefined : items.get(k)
    return a && a.severity === 'critical' && !a.acked ? a : null
  },
  get(key: string): AlarmItem | undefined {
    return items.get(key)
  },
  /** the state alarm the vehicle is currently in (undefined when none) */
  ofVehicle(uav: string): AlarmItem | undefined {
    const k = vehicleKey.get(uav)
    return k === undefined ? undefined : items.get(k)
  },
  size: (): number => items.size,
  clear(): void {
    items.clear()
    vehicleKey.clear()
    commitAlarms()
  },
}
