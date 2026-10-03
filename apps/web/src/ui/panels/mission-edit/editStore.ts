// Route and area editor state (M15-FR-022 mission-edit; AWR-14 §5.3, §6.8 state table; UX-FR-023, UX-FR-024; M10-AC-025;
// D1-AC-17). One editor at a time, for the focused vehicle. Written on user actions only (no periodic writes): every draft
// change pushes the undo history (<= 50 steps), redraws the draft in the viewport (facade mission.setDraft, next frame)
// and schedules the coarse check after input.validateIdleMs (M04 path_coarse_check, buffer 1 m, <= 1000 points).
//   route    CLEAN -> DIRTY on a change; DIRTY -> CLEAN when undone to the start; submit (no violation, within the
//            limits) -> SUBMITTING: AGL heights are converted with ground_dtm, then uav/{id}/cmd/follow_path {waypoints,
//            speed_mps}; accepted shows "fine check", running closes the editor (the draft became the current route),
//            rejected or failed returns to DIRTY with the reason and highlights the first rejected segment.
//   area     clicks add vertices, Enter or a double click closes the ring (a self-intersecting ring is refused), Shift
//            and drag draws a rectangle; the generator form previews (POST /api/missions/preview, drawn only) and
//            creates the mission (POST /api/missions) for the assigned vehicles, then starts it (mission/{mid}/start).
import { useStore } from 'zustand'
import { FlightFlags, Owner } from '@awr/contracts/enums'
import { reasonText, t } from '@/app/i18n'
import { createAwrStore } from '@/lib/createStore'
import { INPUT } from '@/lib/tokens/input.gen'
import { apiGet, apiPost, ApiError, worldQuery } from '@/net/api'
import { rtClient, type CallHandle, type CallResult } from '@/net/rt'
import { mission as vpMission, viewport } from '@/viewport/facade'
import { runService, runVehicleCmd } from '@/ui/actions/vehicleCommands'
import { prefs, prefsStore } from '@/stores/prefs'
import { vehicleState } from '@/ui/actions/admission'
import {
  canAdd, generatorParams, insertAt, midpoint, pushHistory, redo as redoH, removeAt, reorder, ROUTE_LIMITS, routeLength, routePoints,
  sameRoute, selfIntersects, undo as undoH, updateAt, withRef, type AltRef, type AreaGenerator, type AreaParams, type DraftWp, type History,
} from './editModel'

export type EditTool = 'select' | 'add' | 'area'
export type EditPhase = 'CLOSED' | 'CLEAN' | 'DIRTY' | 'SUBMITTING'
export interface Violation { seg: number; reason: string }
export interface CheckState { pending: boolean; ok: boolean | null; violations: readonly Violation[]; error: string | null }
export type SubmitStatus = 'idle' | 'converting' | 'sent' | 'accepted' | 'running' | 'succeeded' | 'failed' | 'rejected'
export interface PreviewPath { vehicle: string; pts: number[][]; lengthM: number; socAfterPct: number | null }
export interface PreviewState {
  status: 'idle' | 'pending' | 'ok' | 'error'
  id: string | null
  paths: readonly PreviewPath[]
  feasible: boolean | null
  infeasible: readonly string[]
  coveragePred: number | null
  error: string | null
}
export interface CreatedMission { mid: string | null; status: 'creating' | 'starting' | 'started' | 'error'; error: string | null }

export interface EditState {
  phase: EditPhase
  vehicle: string | null
  tool: EditTool
  wps: readonly DraftWp[]
  initial: readonly DraftWp[]
  history: History
  sel: number
  /**
   * the waypoint being dragged in the viewport and its live position: drawn and marked at once, written into `wps` (one
   * history step) on release, so the table and the count line do not re-render on every pointer move
   */
  dragging: { i: number; wp: DraftWp } | null
  /** route speed (m/s); null = the vehicle's cruise speed */
  speedMps: number | null
  /** reference for new waypoints */
  ref: AltRef
  check: CheckState
  submit: { status: SubmitStatus; code: number; reason: string | null }
  /** the last submitted route of this page (kept after the editor closes: the detail page shows its result) */
  lastRoute: { vehicle: string; status: SubmitStatus; code: number } | null
  area: { pts: readonly (readonly [number, number])[]; closed: boolean; error: string | null }
  gen: AreaParams & { vehicles: readonly string[] }
  preview: PreviewState
  created: CreatedMission | null
  version: number
}

const IDLE_CHECK: CheckState = { pending: false, ok: null, violations: [], error: null }
const IDLE_PREVIEW: PreviewState = { status: 'idle', id: null, paths: [], feasible: null, infeasible: [], coveragePred: null, error: null }
export const DEFAULT_GEN: AreaParams = { generator: 'lawnmower', aglM: 30, spacingM: 20, speedMps: 5 }

export const editStore = createAwrStore<EditState>('ui.missionEdit', () => ({
  phase: 'CLOSED', vehicle: null, tool: 'select', wps: [], initial: [], history: { past: [], future: [] }, sel: -1, dragging: null, speedMps: null, ref: 'AGL',
  check: IDLE_CHECK, submit: { status: 'idle', code: 0, reason: null }, lastRoute: null,
  area: { pts: [], closed: false, error: null }, gen: { ...DEFAULT_GEN, vehicles: [] }, preview: IDLE_PREVIEW, created: null, version: 0,
}))
export const useEdit = <T,>(sel: (s: EditState) => T): T => useStore(editStore, sel)

const ground = (x: number, y: number): number => viewport.groundZ(x, y)
const get = () => editStore.getState()
function set(p: Partial<EditState>): void {
  editStore.setState({ ...p, version: get().version + 1 })
  syncDraft()
}

/** the waypoints as drawn: `wps` with the live drag position applied */
export function effectiveWps(s: EditState): readonly DraftWp[] {
  const d = s.dragging
  if (!d || !s.wps[d.i]) return s.wps
  const out = [...s.wps]
  out[d.i] = d.wp
  return out
}

/** the viewport draft (route, rejected segments, selection, area, preview paths) */
function syncDraft(): void {
  const s = get()
  if (s.phase === 'CLOSED') {
    vpMission.setDraft(null)
    return
  }
  const route = routePoints(effectiveWps(s), ground)
  const bad = s.check.violations.length ? new Uint8Array(Math.max(0, s.wps.length - 1)) : null
  if (bad) for (const v of s.check.violations) if (v.seg >= 0 && v.seg < bad.length) bad[v.seg] = 1
  const area = s.area.pts.length ? Float64Array.from(s.area.pts.flatMap((p) => [p[0], p[1]])) : null
  const preview = s.preview.paths.map((p) => Float64Array.from(p.pts.flat()))
  vpMission.setDraft({ route, bad, sel: s.sel, area, areaClosed: s.area.closed, preview })
}

// ------------------------------------------------------------------------------------------------ coarse check
let checkTimer: ReturnType<typeof setTimeout> | null = null
let checkSeq = 0
function scheduleCheck(): void {
  if (checkTimer) clearTimeout(checkTimer)
  const s = get()
  if (s.wps.length < ROUTE_LIMITS.minWaypoints) {
    if (s.check.violations.length || s.check.pending || s.check.ok !== null) set({ check: IDLE_CHECK })
    return
  }
  if (!s.check.pending) set({ check: { ...s.check, pending: true } })
  checkTimer = setTimeout(() => void runCheck(), INPUT.validateIdleMs)
}

interface CoarseReply { ok?: boolean; violations?: { seg: number; reason: string }[] }
async function runCheck(): Promise<void> {
  checkTimer = null
  const seq = ++checkSeq
  const s = get()
  const world = viewport.worldId
  if (!world || s.phase === 'CLOSED' || s.wps.length < ROUTE_LIMITS.minWaypoints) return
  const pts = routePoints(s.wps, ground)
  const polyline: number[][] = []
  for (let i = 0; i < pts.length; i += 3) polyline.push([r2(pts[i]), r2(pts[i + 1]), r2(pts[i + 2])])
  try {
    const r = await worldQuery<CoarseReply>(world, { op: 'path_coarse_check', polyline, buffer_m: 1 })
    if (seq !== checkSeq || get().phase === 'CLOSED') return
    const violations = (r.violations ?? []).map((v) => ({ seg: Number(v.seg), reason: String(v.reason) }))
    set({ check: { pending: false, ok: violations.length === 0, violations, error: null } })
  } catch (e) {
    if (seq !== checkSeq) return
    // a failed check never blocks the operator: the server's fine check still runs on submit (ADR-016)
    set({ check: { pending: false, ok: null, violations: [], error: e instanceof ApiError ? `${e.reason ?? e.status}` : String((e as Error)?.message ?? e) } })
  }
}
const r2 = (v: number): number => Math.round(v * 100) / 100

// ------------------------------------------------------------------------------------------------ edits
/** apply a route change: history, DIRTY or CLEAN, coarse check */
function commit(wps: readonly DraftWp[], sel: number = get().sel): void {
  const s = get()
  if (s.phase === 'CLOSED' || s.phase === 'SUBMITTING') return
  const history = pushHistory(s.history, s.wps)
  set({ wps, history, sel: Math.min(sel, wps.length - 1), phase: sameRoute(wps, s.initial) ? 'CLEAN' : 'DIRTY', submit: { status: 'idle', code: 0, reason: null } })
  scheduleCheck()
}

/** current vehicle position (ENU) from the swarm columns of the page RtClient, null when unknown */
export function vehiclePos(id: string | null): [number, number, number] | null {
  const rt = rtClient()
  if (!rt || !id) return null
  const a = rt.roster.agentNoOf(id)
  const sw = rt.swarm
  for (let i = 0; i < sw.n; i++) if (sw.agentNo[i] === a) return [sw.pos[3 * i], sw.pos[3 * i + 1], sw.pos[3 * i + 2]]
  return null
}

export const routeEditor = {
  /** open the editor for one vehicle (operator, single focus), with an empty draft */
  open(vehicle: string, tool: EditTool = 'select'): void {
    if (checkTimer) clearTimeout(checkTimer)
    checkSeq++
    set({
      phase: 'CLEAN', vehicle, tool, wps: [], initial: [], history: { past: [], future: [] }, sel: -1, dragging: null, speedMps: null, check: IDLE_CHECK,
      submit: { status: 'idle', code: 0, reason: null }, area: { pts: [], closed: false, error: null },
      gen: { ...get().gen, vehicles: [vehicle] }, preview: IDLE_PREVIEW, created: null,
    })
  },
  close(): void {
    if (checkTimer) clearTimeout(checkTimer)
    checkSeq++
    set({ phase: 'CLOSED', wps: [], initial: [], sel: -1, dragging: null, check: IDLE_CHECK, area: { pts: [], closed: false, error: null }, preview: IDLE_PREVIEW, tool: 'select' })
  },
  setTool(tool: EditTool): void {
    if (get().phase !== 'CLOSED') set({ tool })
  },
  setRef(ref: AltRef): void {
    set({ ref })
  },
  setSpeed(v: number | null): void {
    set({ speedMps: v })
  },
  select(i: number): void {
    const s = get()
    if (i !== s.sel && i >= -1 && i < s.wps.length) set({ sel: i })
  },
  /** append a waypoint at a ground point: height of the previous waypoint (the first one: the vehicle's height) */
  addAt(x: number, y: number): boolean {
    const s = get()
    if (!canAdd(s.wps.length) || s.phase === 'SUBMITTING') return false
    const prev = s.wps[s.wps.length - 1]
    let wp: Omit<DraftWp, 'id'>
    if (prev) {
      const z = prev.ref === 'WORLD' ? prev.h : ground(prev.x, prev.y) + prev.h
      wp = { x, y, ref: prev.ref, h: prev.ref === 'WORLD' ? z : prev.h }
    } else {
      const p = vehiclePos(s.vehicle)
      const z = p ? Math.max(p[2], ground(x, y) + 2) : ground(x, y) + DEFAULT_AGL_M
      wp = { x, y, ref: s.ref, h: s.ref === 'WORLD' ? z : Math.max(2, z - ground(x, y)) }
    }
    commit(insertAt(s.wps, s.wps.length, wp), s.wps.length)
    return true
  },
  /** insert after waypoint i (table "+"), at the midpoint of segment i (viewport handle), or 10 m beyond the last one */
  insertAfter(i: number): boolean {
    const s = get()
    if (!canAdd(s.wps.length) || !s.wps.length) return false
    const mid = midpoint(s.wps, i, ground)
    if (mid) {
      commit(insertAt(s.wps, i + 1, mid), i + 1)
      return true
    }
    const last = s.wps[s.wps.length - 1]
    commit(insertAt(s.wps, s.wps.length, { ...last, x: last.x + 10 }), s.wps.length)
    return true
  },
  remove(i: number): void {
    const s = get()
    if (i < 0 || i >= s.wps.length) return
    commit(removeAt(s.wps, i), Math.min(i, s.wps.length - 2))
  },
  update(i: number, patch: Partial<Omit<DraftWp, 'id'>>): void {
    const s = get()
    if (i < 0 || i >= s.wps.length) return
    commit(updateAt(s.wps, i, patch), i)
  },
  /** reference change keeps the world height */
  setWpRef(i: number, ref: AltRef): void {
    const s = get()
    const w = s.wps[i]
    if (!w) return
    commit(updateAt(s.wps, i, withRef(w, ref, ground)), i)
  },
  move(i: number, dir: -1 | 1): void {
    const s = get()
    const j = i + dir
    if (j < 0 || j >= s.wps.length) return
    commit(reorder(s.wps, i, dir), j)
  },
  /** arrow keys: 1 m (Shift 10 m) east/north */
  nudge(dx: number, dy: number): void {
    const s = get()
    const w = s.wps[s.sel]
    if (!w) return
    commit(updateAt(s.wps, s.sel, { x: w.x + dx, y: w.y + dy }), s.sel)
  },
  /**
   * a drag in the viewport: `live` positions move the draft without history (drawn next frame); the release commits one
   * history step from the position before the drag
   */
  drag(i: number, patch: Partial<Pick<DraftWp, 'x' | 'y' | 'h'>>, live: boolean, before?: readonly DraftWp[]): void {
    const s = get()
    const w = s.wps[i]
    if (!w) return
    if (live) {
      set({ dragging: { i, wp: { ...(s.dragging?.i === i ? s.dragging.wp : w), ...patch } }, sel: i })
      return
    }
    const wps = updateAt(s.wps, i, { ...(s.dragging?.i === i ? s.dragging.wp : w), ...patch })
    const history = pushHistory(s.history, before ?? s.wps)
    set({ wps, dragging: null, history, sel: i, phase: sameRoute(wps, s.initial) ? 'CLEAN' : 'DIRTY', submit: { status: 'idle', code: 0, reason: null } })
    scheduleCheck()
  },
  undo(): void {
    const s = get()
    const r = undoH(s.history, s.wps)
    if (!r || s.phase === 'SUBMITTING') return
    set({ wps: r.wps, history: r.h, sel: Math.min(s.sel, r.wps.length - 1), phase: sameRoute(r.wps, s.initial) ? 'CLEAN' : 'DIRTY' })
    scheduleCheck()
  },
  redo(): void {
    const s = get()
    const r = redoH(s.history, s.wps)
    if (!r || s.phase === 'SUBMITTING') return
    set({ wps: r.wps, history: r.h, sel: Math.min(s.sel, r.wps.length - 1), phase: sameRoute(r.wps, s.initial) ? 'CLEAN' : 'DIRTY' })
    scheduleCheck()
  },
  /** submit the route (guards: >= 2 points, within ADR-016 limits, no coarse violation, not already submitting) */
  async submit(): Promise<void> {
    const s = get()
    const id = s.vehicle
    if (!id || submitBlockedKey(s) !== null) return
    set({ phase: 'SUBMITTING', submit: { status: 'converting', code: 0, reason: null } })
    let zs: number[]
    try {
      zs = await worldHeights(s.wps)
    } catch (e) {
      set({ phase: 'DIRTY', submit: { status: 'rejected', code: e instanceof ApiError ? (e.reason ?? 0) : 0, reason: t('edit.route.groundFailed') } })
      return
    }
    if (get().phase !== 'SUBMITTING' || get().vehicle !== id) return
    const waypoints = s.wps.map((w, i) => [r2(w.x), r2(w.y), r2(zs[i])])
    const args: Record<string, unknown> = { waypoints }
    if (s.speedMps !== null) args.speed_mps = s.speedMps
    const h = runVehicleCmd('follow_path', id, args, { toastSuccess: true })
    if (!h) {
      set({ phase: 'DIRTY', submit: { status: 'rejected', code: 213, reason: reasonText(213).short } })
      return
    }
    set({ submit: { status: 'sent', code: 0, reason: null }, lastRoute: { vehicle: id, status: 'sent', code: 0 } })
    followRoute(h, id)
  },
}

/** called when a submitted route runs and the editor closed (the page restores the rail width) */
let onRouteRunning: (() => void) | null = null
export function setOnRouteRunning(fn: (() => void) | null): void {
  onRouteRunning = fn
}

/** world z of every waypoint: AGL ones through M04 ground_dtm (<= 64 points per query, AWR-17 §4.3.2) */
async function worldHeights(wps: readonly DraftWp[]): Promise<number[]> {
  const out = wps.map((w) => (w.ref === 'WORLD' ? w.h : Number.NaN))
  const idx = wps.map((w, i) => (w.ref === 'AGL' ? i : -1)).filter((i) => i >= 0)
  const world = viewport.worldId
  for (let k = 0; k < idx.length; k += 64) {
    const part = idx.slice(k, k + 64)
    let zs: (number | null)[] | null = null
    if (world) {
      const r = await worldQuery<{ z_m?: (number | null)[] }>(world, { op: 'ground_dtm', points: part.map((i) => [r2(wps[i].x), r2(wps[i].y)]) })
      zs = r.z_m ?? null
    }
    part.forEach((i, j) => {
      const g = zs?.[j]
      out[i] = (typeof g === 'number' && Number.isFinite(g) ? g : ground(wps[i].x, wps[i].y)) + wps[i].h
    })
  }
  return out
}

/** the result lifecycle of a submitted route (AWR-14 §6.8 state table) */
function followRoute(h: CallHandle, id: string): void {
  h.onResult((r: CallResult) => {
    const s = get()
    const st = r.status as SubmitStatus
    const lastRoute = { vehicle: id, status: st, code: r.code }
    if (s.vehicle !== id || s.phase === 'CLOSED') {
      set({ lastRoute })
      return
    }
    if (r.status === 'accepted') set({ submit: { status: 'accepted', code: 0, reason: null }, lastRoute })
    else if (r.status === 'running' || r.status === 'succeeded') {
      // the draft is now the vehicle's route: the editor closes (CLOSED); the detail page shows the result
      set({ lastRoute, submit: { status: st, code: 0, reason: null } })
      if (s.phase === 'SUBMITTING') {
        routeEditor.close()
        // back to the vehicle's detail page, where the route result stays visible (RouteStatus)
        if (prefsStore.getState().layout.right.page === 'edit') prefs.setLayout({ right: { page: 'detail' } })
        onRouteRunning?.()
      }
    } else {
      const firstBad = typeof (r.detail as { seg?: unknown } | undefined)?.seg === 'number' ? (r.detail as { seg: number }).seg : -1
      const violations = firstBad >= 0 ? [{ seg: firstBad, reason: r.reason ?? String(r.code) }] : s.check.violations
      set({
        phase: 'DIRTY', submit: { status: st, code: r.code, reason: r.code ? reasonText(r.code).short : (r.reason ?? null) }, lastRoute,
        check: { ...s.check, violations },
      })
    }
  })
}

/** i18n key of why submitting is not possible now, or null */
export function submitBlockedKey(s: EditState): string | null {
  if (s.phase === 'SUBMITTING') return 'edit.route.submitting'
  if (s.wps.length < ROUTE_LIMITS.minWaypoints) return 'edit.route.tooFew'
  if (s.wps.length > ROUTE_LIMITS.maxWaypoints) return 'edit.route.tooMany'
  if (routeLength(routePoints(s.wps, ground)) > ROUTE_LIMITS.maxLengthM) return 'edit.route.tooLong'
  if (s.check.violations.length) return 'edit.route.violations'
  if (s.check.pending) return 'edit.route.checking'
  const v = vehicleState(s.vehicle ?? '')
  if (v && (v.flags & FlightFlags.IN_AIR) === 0) return 'admit.airborneFirst'
  return null
}

/** route length (m) for the count line */
export const draftLengthM = (s: EditState): number => routeLength(routePoints(s.wps, ground))

/** default height of a first waypoint when the vehicle position is unknown */
export const DEFAULT_AGL_M = 20

// ------------------------------------------------------------------------------------------------ area
export const areaEditor = {
  addVertex(x: number, y: number): void {
    const s = get()
    const last = s.area.pts[s.area.pts.length - 1]
    // the second click of a double click lands on the same ground point: one vertex
    if (!s.area.closed && last && Math.hypot(last[0] - x, last[1] - y) < 0.5) return
    if (s.area.closed) set({ area: { pts: [[x, y]], closed: false, error: null }, preview: IDLE_PREVIEW, created: null })
    else if (s.area.pts.length < 64) set({ area: { pts: [...s.area.pts, [x, y]], closed: false, error: null }, preview: IDLE_PREVIEW })
  },
  removeLast(): void {
    const s = get()
    if (!s.area.closed && s.area.pts.length) set({ area: { pts: s.area.pts.slice(0, -1), closed: false, error: null } })
  },
  /** close the ring; a self-intersecting or degenerate one is refused with a hint */
  close(): boolean {
    const s = get()
    if (s.area.closed) return true
    if (s.area.pts.length < 3) {
      set({ area: { ...s.area, error: 'edit.area.tooFew' } })
      return false
    }
    if (selfIntersects(s.area.pts)) {
      set({ area: { ...s.area, error: 'edit.area.selfIntersect' } })
      return false
    }
    set({ area: { pts: s.area.pts, closed: true, error: null } })
    return true
  },
  setRect(pts: [number, number][], closed: boolean): void {
    set({ area: { pts, closed, error: null }, preview: IDLE_PREVIEW, created: null })
  },
  clear(): void {
    set({ area: { pts: [], closed: false, error: null }, preview: IDLE_PREVIEW, created: null })
  },
  setGen(p: Partial<EditState['gen']>): void {
    set({ gen: { ...get().gen, ...p }, preview: IDLE_PREVIEW })
  },
  body(): Record<string, unknown> | null {
    const s = get()
    if (!s.area.closed || !s.gen.vehicles.length) return null
    return { generator: s.gen.generator, params: generatorParams(s.area.pts, s.gen), vehicle_ids: [...s.gen.vehicles] }
  },
  /** R26: the server generator's paths (only drawn), energy pre-check per vehicle; 202 is polled through R65 */
  async preview(): Promise<void> {
    const body = areaEditor.body()
    if (!body) return
    set({ preview: { ...IDLE_PREVIEW, status: 'pending' } })
    try {
      let r = await apiPost<PreviewReply>('/api/missions/preview', body)
      for (let k = 0; r.status === 'pending' && r.preview_id && k < 40; k++) {
        await new Promise((ok) => setTimeout(ok, 250))
        r = await apiGet<PreviewReply>(`/api/missions/preview/${r.preview_id}`)
      }
      if (r.status === 'pending') throw new Error('timeout')
      const per = r.energy_precheck?.per_vehicle ?? []
      const infeasible = per.filter((p) => typeof p.soc_after_pct === 'number' && p.soc_after_pct < 20).map((p) => p.id)
      const paths = (r.paths ?? []).map((p) => ({
        vehicle: p.vehicle_id, pts: p.polyline_enu_m ?? [], lengthM: p.length_m ?? 0, socAfterPct: typeof p.soc_after_pct === 'number' ? p.soc_after_pct : null,
      }))
      set({ preview: { status: 'ok', id: r.preview_id ?? null, paths, feasible: r.energy_precheck?.feasible ?? null, infeasible, coveragePred: r.stats?.coverage_pred ?? null, error: null } })
    } catch (e) {
      set({ preview: { ...IDLE_PREVIEW, status: 'error', error: errText(e) } })
    }
  },
  /** R25 then mission/{mid}/start; the created mission row appears in the mission tab */
  async create(): Promise<void> {
    const body = areaEditor.body()
    if (!body) return
    const s = get()
    set({ created: { mid: null, status: 'creating', error: null } })
    try {
      const r = await apiPost<{ mid: string }>('/api/missions', s.preview.id ? { ...body, preview_id: s.preview.id } : body,
        { headers: { 'Idempotency-Key': crypto.randomUUID() } })
      set({ created: { mid: r.mid, status: 'starting', error: null } })
      // vehicles the operator still holds (a route flown from this page): their operator lease is handed back first, so
      // the mission engine can lease them (a MISSION call on an operator-held vehicle is refused and retried forever)
      await releaseOperatorLeases(body.vehicle_ids as string[])
      const h = runService(`mission:${r.mid}:start`, `mission/${r.mid}/start`, {}, t('mission.action', { op: t('mission.op.start'), mid: r.mid }), { toastSuccess: true })
      h?.onResult((res) => {
        const c = get().created
        if (!c || c.mid !== r.mid) return
        if (res.status === 'accepted' || res.status === 'running' || res.status === 'succeeded') set({ created: { ...c, status: 'started' } })
        else set({ created: { ...c, status: 'error', error: res.code ? reasonText(res.code).short : (res.reason ?? res.status) } })
      })
      if (!h) set({ created: { mid: r.mid, status: 'error', error: reasonText(213).short } })
    } catch (e) {
      set({ created: { mid: null, status: 'error', error: errText(e) } })
    }
  },
}

/** hand back the operator lease of the listed vehicles that the operator holds (owner OPERATOR); waits <= 3 s per call */
async function releaseOperatorLeases(ids: readonly string[]): Promise<void> {
  const held = ids.filter((id) => vehicleState(id)?.owner === Owner.OPERATOR)
  await Promise.all(held.map(async (id) => {
    const h = runVehicleCmd('release', id, { return_to: 'none' })
    if (!h) return
    await Promise.race([h.result, new Promise((ok) => setTimeout(ok, 3000))])
  }))
}

interface PreviewReply {
  preview_id?: string
  status?: string
  paths?: { vehicle_id: string; polyline_enu_m?: number[][]; length_m?: number; soc_after_pct?: number }[]
  energy_precheck?: { feasible?: boolean; per_vehicle?: { id: string; soc_after_pct?: number }[] }
  stats?: { coverage_pred?: number | null }
}

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.reason ? `${e.reason} ${reasonText(e.reason).short}` : `${e.status}`
  return String((e as Error)?.message ?? e)
}

export type { AreaGenerator }
