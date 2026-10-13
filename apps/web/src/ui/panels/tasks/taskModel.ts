// Task creation and task table model (R-D2-03 to R-D2-06, R-D2-16, R-D2-19; M15-FR-214; UX-FR-126 to UX-FR-131; AWR-14
// §20.9.1 to §20.9.4): pure helpers of the "new task" page and the Dock task and relay tabs. The task semantics, bounds and
// the plan are the server's (M20, AWR-17 §17.6.7); these helpers only prefill the form (UI defaults of §20.9.1), run the
// instant pre-check of §20.9.2 (the server stays authoritative: codes 110, 580 to 586), build the SB-53 body, compute the
// required GSD shown read-only for patrols (same formula as python awr.tasking.decompose.g_req_m: top-down critical
// dimension sqrt(l w) over the level's N_p90 pixels, floored to mm) and format the rows of the task table.
import type { RelayStats, TaskingTaskPreview } from '@awr/contracts/types'
import { CLASS_DEFAULTS, N_P90, type ReqLevel, type TargetClassName } from '@/app/shell/engineD2'
import { polygonArea, selfIntersects } from '@/ui/panels/mission-edit/editModel'
import type { IconKey } from '@/ui/icons/registry'
import type { TaskRow, TaskSpecBody, TaskState, TaskType } from '@/stores/tasking'

export const TASK_TYPE_ORDER: readonly TaskType[] = ['area_guard', 'area_patrol', 'area_scan_gsd', 'perimeter_patrol', 'polyline_patrol', 'point_watch',
  'relay_watch']
export const TASK_ICON: Readonly<Record<TaskType, IconKey>> = {
  point_watch: 'task.point', polyline_patrol: 'task.polyline', area_patrol: 'task.area', area_scan_gsd: 'task.scan', perimeter_patrol: 'task.perimeter',
  relay_watch: 'task.relay', area_guard: 'task.guard',
}

/** drawing tool of a task type (AWR-14 §20.9.1 column 2) */
export type DrawKind = 'point' | 'line' | 'area' | 'target'
export function drawKindOf(type: TaskType): DrawKind {
  if (type === 'point_watch') return 'point'
  if (type === 'polyline_patrol') return 'line'
  if (type === 'relay_watch') return 'target'
  return 'area'
}

/** server-side size quotas mirrored for the instant hints (AWR-04 §11.5; 17 §17.6.7; code 585 decides) */
export const TASK_LIMITS = { areaMaxM2: 1e6, verticesMax: 64, stripsMax: 200, waypointsMin: 2, waypointsMax: 64 } as const

export type XY = readonly [number, number]
export type XYZ = readonly [number, number, number]

/** the drawn geometry of the form: a point, a polyline (waypoints) or a ring (polygon), or a target id */
export interface TaskGeom {
  kind: DrawKind
  points: readonly XYZ[]
  target: string | null
}
export const EMPTY_GEOM: TaskGeom = { kind: 'area', points: [], target: null }

export type Level = 'D' | 'R' | 'I'
export type OnDetect = 'notify' | 'relay' | 'ignore'
export type Priority = 'low' | 'normal' | 'high'

/** form values of every type (one flat record; buildSpec picks the fields of the type) */
export interface TaskForm {
  label: string
  // point_watch
  altM: number
  altRef: 'agl' | 'abs'
  dwellS: number | null
  look: 'point' | 'heading' | 'target'
  lookTarget: string | null
  loiterRadiusM: number
  // polyline_patrol
  lineMode: 'once' | 'pingpong' | 'loop'
  laps: number
  speedMps: number | null
  facing: 'down' | 'forward' | 'side'
  // area_patrol, area_guard
  nVehicles: 'auto' | number
  targetClass: TargetClassName
  levelReq: Level
  revisitS: number
  durationS: number | null
  // area_scan_gsd
  gsdCm: number
  sidelap: number
  thetaMaxDeg: number
  tMaxS: number | null
  hCapM: number
  clearanceM: number
  multiSortie: boolean
  // perimeter_patrol
  offsetM: number
  direction: 'cw' | 'ccw'
  // relay_watch
  relayLevel: Level | null
  // common (the "more" section)
  priority: Priority
  onDetect: OnDetect
  allowedNests: string[]
  allowedModels: string[]
}

/** UI prefill of AWR-14 §20.9.1 (the bounds and the meaning are the server's) */
export function defaultForm(type: TaskType): TaskForm {
  return {
    label: '', altM: 60, altRef: 'agl', dwellS: 300, look: 'point', lookTarget: null, loiterRadiusM: 80, lineMode: 'loop', laps: 3, speedMps: null,
    facing: 'down', nVehicles: 'auto', targetClass: 'person', levelReq: 'R', revisitS: 300, durationS: type === 'area_patrol' ? 1800 : null, gsdCm: 3,
    sidelap: 0.1, thetaMaxDeg: 15, tMaxS: null, hCapM: 120, clearanceM: 20, multiSortie: false, offsetM: 0, direction: 'ccw', relayLevel: null,
    priority: 'normal', onDetect: type === 'area_guard' ? 'relay' : 'notify', allowedNests: [], allowedModels: [],
  }
}

// ------------------------------------------------------------------------------------------------ geometry helpers
/** counter-clockwise ring (the contract's polygon orientation) */
export function ccw(ring: readonly XY[]): XY[] {
  return polygonArea(ring) < 0 ? [...ring].reverse() : [...ring]
}

export function areaM2(ring: readonly XY[]): number {
  return ring.length >= 3 ? Math.abs(polygonArea(ring)) : 0
}

/** the smallest width of a ring over its edge directions (rotating calipers on the vertices; the strip count estimate) */
export function minWidthM(ring: readonly XY[]): number {
  const n = ring.length
  if (n < 3) return 0
  let best = Number.POSITIVE_INFINITY
  for (let i = 0; i < n; i++) {
    const a = ring[i]
    const b = ring[(i + 1) % n]
    const dx = b[0] - a[0]
    const dy = b[1] - a[1]
    const L = Math.hypot(dx, dy)
    if (L < 1e-9) continue
    let lo = Number.POSITIVE_INFINITY
    let hi = Number.NEGATIVE_INFINITY
    for (const p of ring) {
      const d = ((p[0] - a[0]) * -dy + (p[1] - a[1]) * dx) / L
      lo = Math.min(lo, d)
      hi = Math.max(hi, d)
    }
    best = Math.min(best, hi - lo)
  }
  return Number.isFinite(best) ? best : 0
}

/** total length of a polyline (m, horizontal) */
export function lineLengthM(pts: readonly XY[]): number {
  let s = 0
  for (let i = 1; i < pts.length; i++) s += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
  return s
}

/** required ground sample distance (m/px) of a class and level: sqrt(l w) / N_p90, floored to mm (decompose.g_req_m) */
export function requiredGsdM(cls: TargetClassName, level: Level): number {
  const s = (CLASS_DEFAULTS[cls] as unknown as { size_m: { l: number; w: number } }).size_m
  const g = Math.sqrt(s.l * s.w) / N_P90[level as ReqLevel]
  return Math.floor(g * 1000) / 1000
}

// ------------------------------------------------------------------------------------------------ pre-check (§20.9.2)
export interface Issue {
  /** i18n key under task.issue.* */
  key: string
  /** blocking: "preview" stays disabled */
  block: boolean
  code?: number
  value?: number
}

/** front-end pre-check of AWR-14 §20.9.2 (instant hints; the server decides with 110 and 585) */
export function precheck(type: TaskType, g: TaskGeom, f: TaskForm, wOutPx = 1920): Issue[] {
  const out: Issue[] = []
  const kind = drawKindOf(type)
  if (kind === 'target') {
    if (!g.target) out.push({ key: 'task.issue.noTarget', block: true })
    return out
  }
  if (kind === 'point') {
    if (g.points.length < 1) out.push({ key: 'task.issue.noPoint', block: true })
    return out
  }
  const xy = g.points.map((p) => [p[0], p[1]] as const)
  if (kind === 'line') {
    if (xy.length < TASK_LIMITS.waypointsMin) out.push({ key: 'task.issue.minWaypoints', block: true })
    if (xy.length > TASK_LIMITS.waypointsMax) out.push({ key: 'task.issue.waypoints', block: true, code: 585, value: xy.length })
    return out
  }
  if (xy.length < 3) {
    out.push({ key: 'task.issue.minVertices', block: true })
    return out
  }
  if (xy.length > TASK_LIMITS.verticesMax) out.push({ key: 'task.issue.vertices', block: true, code: 585, value: xy.length })
  if (selfIntersects(xy)) out.push({ key: 'task.issue.selfIntersect', block: true })
  const A = areaM2(xy)
  if (A > TASK_LIMITS.areaMaxM2) out.push({ key: 'task.issue.area', block: true, code: 585, value: A })
  if (type === 'area_scan_gsd') {
    const w = Math.min((wOutPx * f.gsdCm) / 100, 2 * f.hCapM * Math.tan((f.thetaMaxDeg * Math.PI) / 180))
    const strips = w > 0 ? Math.ceil(minWidthM(xy) / ((1 - f.sidelap) * w)) : Number.POSITIVE_INFINITY
    if (strips > TASK_LIMITS.stripsMax) out.push({ key: 'task.issue.strips', block: true, code: 585, value: strips })
  }
  return out
}

export const blocking = (issues: readonly Issue[]): boolean => issues.some((i) => i.block)

// ------------------------------------------------------------------------------------------------ request body (SB-53)
const r1 = (x: number): number => Math.round(x * 10) / 10

/** the SB-53 body of a form (params of 17 §17.6.7; common keys only when they differ from the server default) */
export function buildSpec(type: TaskType, g: TaskGeom, f: TaskForm): TaskSpecBody {
  const ring = (): number[][] => ccw(g.points.map((p) => [p[0], p[1]] as const)).map((p) => [r1(p[0]), r1(p[1])])
  let params: Record<string, unknown>
  switch (type) {
    case 'point_watch': {
      const p = g.points[0] ?? [0, 0, 0]
      params = { point: [r1(p[0]), r1(p[1]), r1(p[2])], alt_m: f.altM, alt_ref: f.altRef, dwell_s: f.dwellS, loiter_radius_m: f.loiterRadiusM }
      if (f.look === 'target' && f.lookTarget) params.look = { mode: 'target', target: f.lookTarget }
      else if (f.look === 'heading') params.look = { mode: 'heading' }
      break
    }
    case 'polyline_patrol':
      params = { waypoints: g.points.map((p) => [r1(p[0]), r1(p[1]), r1(p[2])]), mode: f.lineMode, laps: f.laps, alt_m: f.altM, sensor_facing: f.facing,
        auto_detour: true, ...(f.speedMps ? { speed_mps: f.speedMps } : {}), ...(f.durationS ? { duration_s: f.durationS } : {}) }
      break
    case 'area_patrol':
      params = { polygon: ring(), n_vehicles: f.nVehicles, target_class: f.targetClass, level_req: f.levelReq, revisit_s: f.revisitS, duration_s: f.durationS }
      break
    case 'area_scan_gsd':
      params = { polygon: ring(), gsd_cm: f.gsdCm, sidelap: f.sidelap, theta_max_deg: f.thetaMaxDeg, h_cap_m: f.hCapM, clearance_m: f.clearanceM,
        allow_multi_sortie: f.multiSortie, n_vehicles: f.nVehicles, ...(f.tMaxS ? { t_max_s: f.tMaxS } : {}) }
      break
    case 'perimeter_patrol':
      params = { polygon: ring(), offset_m: f.offsetM, direction: f.direction, revisit_s: f.revisitS, n_vehicles: f.nVehicles }
      break
    case 'relay_watch':
      params = { target: g.target ?? '', duration_s: f.durationS, ...(f.relayLevel ? { level_req: f.relayLevel } : {}) }
      break
    case 'area_guard':
      params = { polygon: ring(), target_class: f.targetClass, level_req: f.levelReq, revisit_s: f.revisitS }
      break
  }
  const common: Record<string, unknown> = {}
  if (f.priority !== 'normal') common.priority = f.priority
  if (f.onDetect !== (type === 'area_guard' ? 'relay' : 'notify')) common.on_detect = f.onDetect
  if (f.allowedNests.length) common.allowed_nests = [...f.allowedNests]
  if (f.allowedModels.length) common.allowed_models = [...f.allowedModels]
  const label = f.label.trim()
  return { type, label: label ? label.slice(0, 32) : null, params, ...(Object.keys(common).length ? { common } : {}) }
}

// ------------------------------------------------------------------------------------------------ preview card (§20.9.3)
export interface PreviewSummary {
  n: number
  makespanMin: number | null
  tMaxMin: number | null
  /** "why n" (makespan rule: n - 1 vehicles exceed T_max; fleet_short; user_fixed) */
  why: { rule: string; nMinus1Min: number | null } | null
  curve: { n: number; min: number }[]
  models: string[]
  nests: string[]
}

export function previewSummary(p: TaskingTaskPreview, modelOf: (uav: string) => string | null = () => null): PreviewSummary {
  const min = (s: number | null | undefined): number | null => (typeof s === 'number' && Number.isFinite(s) ? s / 60 : null)
  const curve = (p.makespan_curve ?? []).filter((c) => typeof c.makespan_s === 'number').map((c) => ({ n: c.n, min: (c.makespan_s as number) / 60 }))
  const models = new Set<string>()
  const nests = new Set<string>()
  for (const a of p.assignments ?? []) {
    const m = modelOf(a.uav)
    if (m) models.add(m)
    if (a.nest) nests.add(a.nest)
  }
  return {
    n: p.n_vehicles, makespanMin: min(p.makespan_s), tMaxMin: min(p.t_max_s),
    why: p.why_n ? { rule: p.why_n.rule, nMinus1Min: min(p.why_n.n_minus_1_makespan_s) } : null, curve, models: [...models], nests: [...nests],
  }
}

/** one-line summary of the work items of an assignment ("strips 1-4", "station st-1", ...) */
export function itemsText(items: readonly { kind: string; ref?: string; wid: string }[], kindLabel: (k: string) => string): string {
  const by = new Map<string, string[]>()
  for (const it of items) {
    const l = by.get(it.kind) ?? []
    l.push(it.ref ?? it.wid)
    by.set(it.kind, l)
  }
  return [...by.entries()].map(([k, refs]) => `${kindLabel(k)} ${refs.length > 3 ? `${refs[0]} ... ${refs[refs.length - 1]} (${refs.length})` : refs.join(', ')}`).join(' · ')
}

// ------------------------------------------------------------------------------------------------ task table (§20.9.4)
export type TaskFilter = 'all' | 'active' | 'ended' | 'infeasible'
export const ACTIVE_STATES: readonly TaskState[] = ['PLANNING', 'READY', 'RUNNING', 'PAUSED']

export function filterTasks(rows: readonly TaskRow[], f: TaskFilter): TaskRow[] {
  if (f === 'all') return [...rows]
  if (f === 'active') return rows.filter((r) => ACTIVE_STATES.includes(r.state))
  if (f === 'infeasible') return rows.filter((r) => r.state === 'INFEASIBLE' || r.state === 'FAILED')
  return rows.filter((r) => r.state === 'DONE' || r.state === 'CANCELED')
}

/** the progress shown in the 20-tick bar (0-100): scan = visible-cell coverage, patrols = coverage of the latest visits */
export function progressOf(r: Pick<TaskRow, 'type' | 'progressPct' | 'coveragePct' | 'relay'>): number {
  if (r.type === 'relay_watch' && r.relay) return Math.round(r.relay.coverage * 1000) / 10
  const v = r.coveragePct ?? r.progressPct
  return Math.max(0, Math.min(100, Number.isFinite(v) ? v : 0))
}

/** the latest FAILED or INFEASIBLE row (the table's one hot cell, AWR-14 §20.15.3) */
export function hotRow(rows: readonly TaskRow[]): string | null {
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i].state === 'FAILED' || rows[i].state === 'INFEASIBLE') return rows[i].tid
  return null
}

/** which row operations apply to a state (start, pause, resume, cancel) */
export function opsFor(state: TaskState): { start: boolean; pause: boolean; resume: boolean; cancel: boolean } {
  return { start: state === 'READY', pause: state === 'RUNNING', resume: state === 'PAUSED', cancel: ACTIVE_STATES.includes(state) }
}

/** vehicles currently assigned to the work items of a task (camera focus, assignment rows) */
export function vehiclesOf(r: Pick<TaskRow, 'items'>): string[] {
  const out = new Set<string>()
  for (const it of r.items) if (it.uav) out.add(it.uav)
  return [...out]
}

/** a warning code of the status (`tasking.warning.<code>`, prefixed `unassigned:`, `relay_failed:`, `child_failed:`) */
export function warningKey(w: string): { key: string; arg: string | null } {
  const m = /^(unassigned|relay_failed|child_failed):(.+)$/.exec(w)
  if (m) return { key: `tasking.warning.${m[1]}`, arg: m[2] }
  return { key: /^[A-Za-z_]+$/.test(w) ? `tasking.warning.${w}` : 'tasking.warning.other', arg: w }
}

// ------------------------------------------------------------------------------------------------ relay tab (§20.9.4)
/** seconds as m:ss (relief countdown, gaps) */
export function mmss(s: number | null | undefined): string {
  if (typeof s !== 'number' || !Number.isFinite(s) || s < 0) return '-'
  const t = Math.round(s)
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const ss = t % 60
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}` : `${m}:${String(ss).padStart(2, '0')}`
}

/** time left of the next relief, counted down between two 1 Hz stats from the wall time of the message */
export function reliefLeftS(etaS: number | null | undefined, msgWallMs: number, nowMs: number): number | null {
  if (typeof etaS !== 'number' || !Number.isFinite(etaS)) return null
  return Math.max(0, etaS - (nowMs - msgWallMs) / 1000)
}

export const GAP_CAUSES = ['keepout_priority', 'illum_transition', 'infeasible', 'battery', 'other'] as const

export interface StationRow { station: string; role: string; uav: string | null; sinceSimS: number | null; pos: readonly number[] | null }

/** stations of the plan geometry joined with the on-station list of the statistics (pure; tests); sinceSimS is the sim
 *  time (s) the vehicle took the station */
export function stationRows(stats: Pick<RelayStats, 'on_station'> | null, stations: readonly { id: string; role: string; uav?: string | null; pos: readonly number[] }[] | undefined): StationRow[] {
  const on = new Map((stats?.on_station ?? []).map((o) => [o.station, o]))
  const since = (ns: number | string | undefined): number | null => (ns !== undefined ? Number(ns) / 1e9 : null)
  const rows: StationRow[] = []
  const seen = new Set<string>()
  for (const st of stations ?? []) {
    const o = on.get(st.id)
    seen.add(st.id)
    rows.push({ station: st.id, role: st.role, uav: o?.uav ?? null, sinceSimS: since(o?.since_t_sim_ns), pos: st.pos })
  }
  for (const o of stats?.on_station ?? []) {
    if (seen.has(o.station)) continue
    rows.push({ station: o.station, role: 'primary', uav: o.uav, sinceSimS: since(o.since_t_sim_ns), pos: null })
  }
  return rows
}
