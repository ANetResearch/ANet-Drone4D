// Safety store (M09 §8, §9.1; M09 §14 item 7: owner M09). Vanilla zustand store for the selected vehicle's safety row
// (topic uav/{id}/safety, awr.uav.safety.v1, 5 Hz subscription) and the alarm entries that feed the alarm centre, the
// Toast merge (14 §11.4: one toast per merge key, <= 3 visible) and the one-red arbitration (M06 viewport/bindings/
// redOwner.ts reads alarms[].{active, severity, vehicleId, rank, tWallMs}).
// Severity mapping (M09-FR-102; 17 §6.12; 14 §11.1): wire level 0/1 -> info, 2 -> warning, 3 -> critical; the catalogue
// rt/safety_codes.json (generated @awr/contracts/safetyCodes) gives class, icon key and merge key per code. Events and
// rows carry codes and parameters only; UI text comes from i18n keys (M09-FR-104), so this store holds no copy.
// RESTORED codes (info) close the open alarms of the same event type for that vehicle; uav.state events are not alarms.
// Writes are coalesced: ingestion only touches module buffers, flushSafety() publishes at most every
// SAFETY_FLUSH_MS (250 ms, Tier S <= 4 writes/s, M15-AC-021); bindSafety() wires the page RtClient and the selection.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { SAFETY_CODES } from '@awr/contracts/safetyCodes'
import type { DataMsg, RtEvent } from '@/net/rt'

export type Severity = 'info' | 'warning' | 'critical'

export interface SafetyAlarm {
  /** `${vehicleId}|${code}` (fleet-wide events: `*|${code}`) */
  key: string
  code: string
  /** event type, e.g. safety.link */
  kind: string
  severity: Severity
  level: 1 | 2 | 3
  /** "one red" rank (M09 §6.3.4): arbiter rank of the target state, or the current severity when there is no transition */
  rank: number
  vehicleId: string | null
  mergeKey: string | null
  icon: string
  /** wall clock ms of the latest occurrence */
  tWallMs: number
  tSimNs: number
  /** occurrences while open */
  count: number
  active: boolean
  acked: boolean
  from: string | null
  to: string | null
  value: number | null
  threshold: number | null
  detail: string | null
}

export interface SafetyToast {
  /** merge key (or the code when the catalogue has none) */
  key: string
  code: string
  severity: Severity
  icon: string
  vehicles: string[]
  count: number
  tWallMs: number
}

/** awr.uav.safety.v1 (17 §6.5; M09 §6.3.5). Optional fields are omitted by the server when they hold defaults. */
export interface SafetyRow {
  active: { code: string; level: number; since_t_ns: number }[]
  fsm: { state: string; sub: string; latched: string[]; auto?: boolean; since_t_ns?: number; reason?: string }
  geofence_margin_m: number
  separation_m: number | null
  battery_rtl: { t_rem_s: number; t_rtl_s: number } | null
  energy?: { soc_pct?: number; soc_rtl_pct?: number; z_rtl_m?: number }
  link?: { src?: 'self' | 'seat' | 'agent'; policy?: 'hold_rtl' | 'ignore'; state?: 'OK' | 'DEGRADED' | 'LOST_HOLD' | 'LOST_RTL'; age_ms?: number | null }
  zone_id?: string | null
  clearance_m?: number
  sep_mate?: string | null
  resume?: { ok: boolean; blocked_by: string | null }
  correct_target_enu_m?: [number, number, number] | null
  faults?: { fault_id: string; kind: string; since_t_ns: number; params?: Record<string, unknown> }[]
}

export interface SafetySelected {
  vehicleId: string
  row: SafetyRow | null
  /** FlightState name of the row (fsm.state) */
  flightState: string | null
  /** highest active condition code (by level), null when none */
  guard: string | null
  geofence: 'inside' | 'near' | 'outside' | null
  batteryPct: number | null
  /** RTL reserve line of the battery gauge (energy.soc_rtl_pct, 14 §10.1) */
  rtlReservePct: number | null
  /** t_rem / (1.3 * t_rtl): < 1 triggers the energy RTL */
  energyMargin: number | null
  resumeOk: boolean
  resumeBlockedBy: string | null
  latched: string[]
  /** true when no row arrived for SAFETY_STALE_MS */
  stale: boolean
  tWallMs: number
}

export interface SafetyState {
  selected: SafetySelected | null
  alarms: readonly SafetyAlarm[]
  /** merged toasts, newest first, at most TOAST_MAX */
  toasts: readonly SafetyToast[]
  unackedCritical: number
  version: number
}

export const SAFETY_FLUSH_MS = 250
export const SAFETY_STALE_MS = 3000
export const TOAST_WINDOW_MS = 3000
export const TOAST_MAX = 3
export const ALARMS_MAX = 2048
const RTL_MARGIN = 1.3
const NEAR_M = 5

export const safetyStore = createAwrStore<SafetyState>('safety', () => ({ selected: null, alarms: [], toasts: [], unackedCritical: 0, version: 0 }))

export function useSafety<T>(sel: (s: SafetyState) => T): T {
  return useStore(safetyStore, sel)
}

export function severityOfLevel(level: number): Severity {
  return level >= 3 ? 'critical' : level === 2 ? 'warning' : 'info'
}

function num(x: unknown): number | null {
  return typeof x === 'number' && Number.isFinite(x) ? x : null
}

function stateLabel(x: unknown): string | null {
  if (x && typeof x === 'object' && 'state' in x) {
    const o = x as { state?: unknown; sub?: unknown }
    const st = typeof o.state === 'string' ? o.state : ''
    const sub = typeof o.sub === 'string' ? o.sub : typeof o.sub === 'number' ? String(o.sub) : ''
    return `${st}/${sub}`
  }
  return typeof x === 'string' ? x : null
}

/** A safety.* event -> alarm entry (null for other event types). */
export function alarmFromEvent(ev: RtEvent, nowMs: number): SafetyAlarm | null {
  if (!ev.type.startsWith('safety.')) return null
  const d = ev.data ?? {}
  const code = typeof d.code === 'string' ? d.code : ev.type
  const info = SAFETY_CODES[code]
  const level = (Math.min(3, Math.max(1, ev.level || info?.level || 1)) as 1 | 2 | 3)
  const vehicleId = ev.uav ?? null
  return {
    key: `${vehicleId ?? '*'}|${code}`, code, kind: ev.type, severity: severityOfLevel(level), level,
    rank: num(d.rank) ?? 0, vehicleId, mergeKey: info?.mergeKey ?? null, icon: info?.icon ?? 'alert.info',
    tWallMs: nowMs, tSimNs: ev.t_sim_ns, count: 1, active: info?.cls !== 'info', acked: false,
    from: stateLabel(d.from), to: stateLabel(d.to), value: num(d.value), threshold: num(d.threshold),
    detail: typeof d.detail === 'string' ? d.detail : null,
  }
}

// ------------------------------------------------------------------ module buffers (no store write on ingestion)
const open = new Map<string, SafetyAlarm>()
const order: string[] = []
let toastBuf: SafetyToast[] = []
let selectedBuf: SafetySelected | null = null
let dirty = false
let lastFlush = 0
let timer: ReturnType<typeof setTimeout> | null = null

function markDirty(nowMs: number): void {
  dirty = true
  if (timer !== null) return
  const wait = Math.max(0, lastFlush + SAFETY_FLUSH_MS - nowMs)
  timer = setTimeout(() => {
    timer = null
    flushSafety(performance.now())
  }, wait)
}

function pushToast(a: SafetyAlarm): void {
  if (a.severity === 'info' && a.code !== 'SAF.OP.RESUME') return
  const key = a.mergeKey ?? a.code
  const hit = toastBuf.find((t) => t.key === key && a.tWallMs - t.tWallMs <= TOAST_WINDOW_MS)
  if (hit) {
    if (a.vehicleId && !hit.vehicles.includes(a.vehicleId)) hit.vehicles.push(a.vehicleId)
    hit.count += 1
    hit.tWallMs = a.tWallMs
    if (a.level === 3) hit.severity = 'critical'
    return
  }
  toastBuf.unshift({ key, code: a.code, severity: a.severity, icon: a.icon, vehicles: a.vehicleId ? [a.vehicleId] : [], count: 1, tWallMs: a.tWallMs })
  if (toastBuf.length > TOAST_MAX) toastBuf.length = TOAST_MAX
}

/** Ingest one event batch (RtClient.onEvents); returns the number of safety events taken. */
export function ingestSafetyEvents(batch: readonly RtEvent[], nowMs: number): number {
  let n = 0
  for (const ev of batch) {
    const a = alarmFromEvent(ev, nowMs)
    if (!a) continue
    n++
    const info = SAFETY_CODES[a.code]
    if (info?.cls === 'info' && a.code.endsWith('RESTORED')) {
      // close the open alarms of the same type for this vehicle
      for (const al of open.values()) if (al.active && al.vehicleId === a.vehicleId && al.kind === a.kind) al.active = false
    }
    const cur = open.get(a.key)
    if (cur) {
      cur.count += 1
      cur.tWallMs = nowMs
      cur.tSimNs = a.tSimNs
      cur.rank = a.rank
      cur.active = a.active || cur.active
      cur.acked = cur.acked && !a.active
      cur.value = a.value
      cur.threshold = a.threshold
      cur.detail = a.detail
      if (a.to) {
        cur.from = a.from
        cur.to = a.to
      }
      const i = order.indexOf(a.key)
      if (i >= 0) order.splice(i, 1)
    } else {
      open.set(a.key, a)
    }
    order.push(a.key)
    pushToast(a)
  }
  while (order.length > ALARMS_MAX) {
    const k = order.findIndex((x) => !open.get(x)?.active)
    const drop = order.splice(k >= 0 ? k : 0, 1)[0]
    open.delete(drop)
  }
  if (n) markDirty(nowMs)
  return n
}

/** Selected vehicle's safety row (uav/{id}/safety). */
export function setSafetyRow(vehicleId: string, row: SafetyRow | null, nowMs: number): void {
  if (!row) {
    selectedBuf = { vehicleId, row: null, flightState: null, guard: null, geofence: null, batteryPct: null, rtlReservePct: null,
      energyMargin: null, resumeOk: true, resumeBlockedBy: null, latched: [], stale: true, tWallMs: nowMs }
    markDirty(nowMs)
    return
  }
  let guard: string | null = null
  let best = 0
  for (const c of row.active ?? []) {
    if (c.level > best) {
      best = c.level
      guard = c.code
    }
  }
  const m = row.geofence_margin_m
  const br = row.battery_rtl
  selectedBuf = {
    vehicleId, row, flightState: row.fsm?.state ?? null, guard,
    geofence: typeof m !== 'number' ? null : m < 0 ? 'outside' : m < NEAR_M ? 'near' : 'inside',
    batteryPct: num(row.energy?.soc_pct), rtlReservePct: num(row.energy?.soc_rtl_pct),
    energyMargin: br && br.t_rtl_s > 0 ? br.t_rem_s / (RTL_MARGIN * br.t_rtl_s) : null,
    resumeOk: row.resume?.ok ?? true, resumeBlockedBy: row.resume?.blocked_by ?? null, latched: row.fsm?.latched ?? [],
    stale: false, tWallMs: nowMs,
  }
  markDirty(nowMs)
}

export function ackAlarm(key: string, nowMs = performance.now()): void {
  const a = open.get(key)
  if (a && !a.acked) {
    a.acked = true
    markDirty(nowMs)
  }
}

export function ackAllAlarms(nowMs = performance.now()): void {
  for (const a of open.values()) a.acked = true
  markDirty(nowMs)
}

export function dismissToast(key: string, nowMs = performance.now()): void {
  toastBuf = toastBuf.filter((t) => t.key !== key)
  markDirty(nowMs)
}

/** Publish buffered changes (one store write). Also marks the selected row stale after SAFETY_STALE_MS. */
export function flushSafety(nowMs: number): boolean {
  if (selectedBuf && !selectedBuf.stale && nowMs - selectedBuf.tWallMs > SAFETY_STALE_MS) {
    selectedBuf = { ...selectedBuf, stale: true }
    dirty = true
  }
  if (!dirty) return false
  dirty = false
  lastFlush = nowMs
  const alarms = order.map((k) => ({ ...open.get(k)! })).reverse()
  let unacked = 0
  for (const a of alarms) if (a.active && !a.acked && a.severity === 'critical') unacked++
  const s = safetyStore.getState()
  safetyStore.setState({ selected: selectedBuf, alarms, toasts: toastBuf.map((t) => ({ ...t, vehicles: [...t.vehicles] })),
    unackedCritical: unacked, version: s.version + 1 })
  return true
}

/** Test helper and scenario reset (sim.reset). */
export function resetSafety(): void {
  open.clear()
  order.length = 0
  toastBuf = []
  selectedBuf = null
  dirty = false
  lastFlush = 0
  if (timer !== null) clearTimeout(timer)
  timer = null
  safetyStore.setState({ selected: null, alarms: [], toasts: [], unackedCritical: 0, version: safetyStore.getState().version + 1 })
}

/** What the binding needs from the realtime client (tests pass a stub). */
export interface SafetySource {
  onEvents(cb: (b: readonly RtEvent[]) => void): () => void
  onData(cb: (m: DataMsg) => void): () => void
  subscribe(topic: string, o: { rate: number }): () => void
}

/** Primary selection source (stores/selection.ts). */
export interface SelectionSource {
  getState(): { primary: string | null }
  subscribe(cb: (s: { primary: string | null }) => void): () => void
}

/** Wire the page RtClient: all safety events, and uav/{primary}/safety at 5 Hz (subscription follows the selection). */
export function bindSafety(rt: SafetySource, selection: SelectionSource, now: () => number = () => performance.now()): () => void {
  let primary: string | null = null
  let offTopic: (() => void) | null = null
  const follow = (id: string | null): void => {
    if (id === primary) return
    offTopic?.()
    offTopic = null
    primary = id
    if (id) {
      offTopic = rt.subscribe(`uav/${id}/safety`, { rate: 5 })
      setSafetyRow(id, null, now())
    } else {
      selectedBuf = null
      markDirty(now())
    }
  }
  const offEv = rt.onEvents((b) => {
    for (const e of b) if (e.type === 'sim.reset') resetSafety()
    ingestSafetyEvents(b, now())
  })
  const offData = rt.onData((m) => {
    if (!primary || m.topic !== `uav/${primary}/safety`) return
    const row = m.data as SafetyRow | null
    if (row && typeof row === 'object' && 'fsm' in row) setSafetyRow(primary, row, now())
  })
  const offSel = selection.subscribe((s) => follow(s.primary))
  follow(selection.getState().primary)
  return () => {
    offEv()
    offData()
    offSel()
    offTopic?.()
    offTopic = null
  }
}
