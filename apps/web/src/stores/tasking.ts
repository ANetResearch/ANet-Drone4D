// Tasking store (M20-FR-006, FR-062, FR-065, FR-080, FR-082, FR-084; M20 PRD §8 item 1; AWR-04 §10.3, §10.5; ADR-110: owner
// M20, M15 writes the panels). Nests, nest status, tasks, plan geometry, dry-run previews and relay statistics of the
// current endpoint, read by the nests, task and relay panels, the viewport layer (viewport/layers/tasking.tsx) and the
// compact phone and tablet views (taskingSummary, quick actions).
// Fields:
//   mode            'sandbox' (REST resources and writes of the session) | 'shared' (shared show: topics only, read only)
//   canEdit         the connection's role is operator or admin (the D2.1 full-function identity included) and the
//                   session is not stale; viewers keep every read and the dry-run (AWR-04 §10.3)
//   nests[]         nest objects (awr.tasking.nest.v1, SB-47) sorted by id; nestStatus[id] the 1 Hz awr.nest.status.v1
//   tasks[]         one row per task: the 1 Hz awr.task.status.v1 merged with the task object of SB-52 (label, params,
//                   parent); plans[tid] the plan geometry and assignments of SB-54 include=plan, refetched when the
//                   status revision grows (17 §17.7.4)
//   preview         the active dry-run (awr.tasking.preview.v1 or its 422 problem) drawn by the layer until cleared;
//                   previewKeepouts the keepout cylinders of the snapshot taken with it (dashed neutral band)
//   relay[tid]      the latest awr.relay.stats.v1; relayHistory[tid] the last 600 samples (1 Hz, P1 coverage curve)
//   selected        the focused task (layer highlight, panel detail)
//   stale           D2.1: the session was reclaimed (energy-saving mode) or the connection is down: the last data stays
//                   on screen, writes are disabled until the session is live again
// Writers: a reference-counted feed (useTasking and the layer attach it) subscribes `nest/*/status`, `task/*/status` and
// `relay/*/stats` at 1 Hz, keeps the messages in module buffers and mirrors them at the shared UI tick (<= 4 writes/s on
// Tier S, ADR-066) only when something changed; REST refreshes (sandbox) run from the same tick: nests and tasks on
// attach, on an unknown id and every 10 s, a plan when its revision grew (one request per task in flight, >= 2 s apart).
// An endpoint change (another sid after a resume, or back to the shared show) resets the store. Actions go through the
// sandbox REST (SB-47 to SB-58: one problem+json per failure, `code` and `detail` kept for the forms) and fall back to the
// WS calls of the same commands on the shared show (nest/*, task/create, task/{tid}/{op}); text from the wire passes
// lib/sanitize.ts.
// D2.2-W1: nests, tasks, plans and relay statistics belong to the world of the realtime source (stores/worldScope): topics
// and REST answers are taken only while that world is the one the view shows (an answer that started before a world or
// source switch is dropped), and the switch resets the store like an endpoint change.
import type {
  Nest, NestStatus, PlanReply, RelayStats, Sustainability, TaskingSnapshot, TaskingTask, TaskingTaskPlan, TaskingTaskPreview,
} from '@awr/contracts/types'
import { useEffect } from 'react'
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { sanitizeText } from '@/lib/sanitize'
import { loop, type FrameCtx } from '@/engine/loop'
import { ApiError, apiDelete, apiGet, apiPatch, apiPost } from '@/net/api'
import { apiPath, getEndpoint, onEndpointChange } from '@/net/endpoint'
import { rtClient, type CallResult, type ConnState, type DataMsg, type RtClient, type RtEvent } from '@/net/rt'
import { uiTickDue } from './uiTick'
import { onWorldReset, worldLive, worldScopeNow } from './worldScope'

export const TASKING_STORE_HZ = 4
export const NESTS_MAX = 4
export const TASKS_ACTIVE_MAX = 6
export const RELAY_HISTORY = 600
export const REFRESH_MS = 10_000
export const PLAN_MIN_GAP_MS = 2_000
export const PREVIEW_POLL_MS = 1_000

export type TaskType = TaskingTask['type']
export type TaskState = TaskingTask['state']
export type NestKind = NonNullable<Nest['kind']>
export const TASK_TYPES: readonly TaskType[] = ['point_watch', 'polyline_patrol', 'area_patrol', 'area_scan_gsd', 'perimeter_patrol', 'relay_watch',
  'area_guard']
export const TERMINAL: readonly TaskState[] = ['DONE', 'CANCELED', 'FAILED', 'INFEASIBLE']
export const ACTIVE: readonly TaskState[] = ['PLANNING', 'READY', 'RUNNING', 'PAUSED']

export type TaskGeometry = NonNullable<TaskingTaskPlan['geometry']>
export type Vec3 = [number, number, number]

/** awr.task.status.v1 (rt/payloads/task_status.schema.json; the generated TS name collides with the agent TaskStatus) */
export interface TaskStatusMsg {
  tid: string
  type: TaskType
  state: TaskState
  revision: number
  t_sim_ns?: number
  progress?: { pct?: number; coverage_pct?: number; laps_done?: number; dwell_s?: number }
  n_vehicles?: number
  makespan_s?: number | null
  items?: { wid: string; kind: string; uav?: string | null; state: string; eta_s?: number | null; reason?: string | null }[]
  children?: string[]
  warnings?: string[]
}

export interface TaskRow {
  tid: string
  type: TaskType
  label: string
  state: TaskState
  revision: number
  parent: string | null
  children: string[]
  progressPct: number
  coveragePct: number | null
  nVehicles: number
  makespanS: number | null
  items: NonNullable<TaskStatusMsg['items']>
  warnings: string[]
  params: Record<string, unknown>
  /** relay tasks: the latest statistics summary */
  relay: { target: string; coverage: number; gaps: number; maxGapS: number; discovered: number; handovers: number; onStation: string[] } | null
}

export interface TaskPlanEntry {
  revision: number
  geometry: TaskGeometry
  assignments: NonNullable<TaskingTaskPlan['assignments']>
}

/** dry-run result: the preview, or the infeasible problem (code 580, 581, 583, 585 or 110 with detail.remedies) */
export interface PreviewEntry {
  spec: TaskSpecBody
  state: 'pending' | 'done' | 'infeasible' | 'error'
  previewId: string | null
  preview: TaskingTaskPreview | null
  code: number
  detail: Record<string, unknown> | null
  tWallMs: number
}

export interface RelaySample { tMonitorS: number; coverage: number; gaps: number; maxGapS: number }

export interface KeepoutCyl { id: string; x: number; y: number; r: number }

export interface TaskSpecBody {
  type: TaskType
  label?: string | null
  params: Record<string, unknown>
  common?: Record<string, unknown>
  preview_id?: string | null
}

export interface TaskingState {
  mode: 'sandbox' | 'shared'
  canEdit: boolean
  nests: Nest[]
  nestStatus: Readonly<Record<string, NestStatus>>
  tasks: TaskRow[]
  plans: Readonly<Record<string, TaskPlanEntry>>
  preview: PreviewEntry | null
  previewKeepouts: KeepoutCyl[]
  relay: Readonly<Record<string, RelayStats>>
  relayHistory: Readonly<Record<string, readonly RelaySample[]>>
  selected: string | null
  stale: boolean
  lastError: { code: number; message: string; detail: unknown } | null
  version: number
}

function blank(): TaskingState {
  return { mode: getEndpoint().kind === 'sandbox' ? 'sandbox' : 'shared', canEdit: false, nests: [], nestStatus: {}, tasks: [], plans: {}, preview: null,
    previewKeepouts: [], relay: {}, relayHistory: {}, selected: null, stale: false, lastError: null, version: 0 }
}

export const taskingStore = createAwrStore<TaskingState>('tasking', blank)

// ------------------------------------------------------------------------------------------------ REST port (tests inject)
export interface TaskingPort {
  get<T>(path: string): Promise<T>
  post<T>(path: string, body: unknown, headers?: Record<string, string>): Promise<T>
  patch<T>(path: string, body: unknown): Promise<T>
  del(path: string): Promise<number>
}
const defaultPort: TaskingPort = {
  get: (p) => apiGet(apiPath(p)),
  post: (p, b, h) => apiPost(apiPath(p), b, h ? { headers: h } : undefined),
  patch: (p, b) => apiPatch(apiPath(p), b),
  del: (p) => apiDelete(apiPath(p)),
}
let port: TaskingPort = defaultPort
/** tests: replace the REST port (null restores net/api) */
export function configureTaskingPort(p: TaskingPort | null): void {
  port = p ?? defaultPort
}

// ------------------------------------------------------------------------------------------------ feed buffers
const buf = {
  nestStatus: new Map<string, NestStatus>(),
  status: new Map<string, TaskStatusMsg>(),
  relay: new Map<string, RelayStats>(),
  history: new Map<string, RelaySample[]>(),
  objs: new Map<string, TaskingTask>(),
  nests: [] as Nest[],
  plans: new Map<string, TaskPlanEntry>(),
  dirtyPlan: new Set<string>(),
  planInflight: new Set<string>(),
  planAtMs: new Map<string, number>(),
  unknownNest: false,
  unknownTask: false,
  refreshAtMs: 0,
  changed: false,
  conn: 'IDLE' as ConnState,
  role: 'viewer' as 'viewer' | 'operator' | 'admin',
}

function resetBuffers(): void {
  buf.nestStatus.clear()
  buf.status.clear()
  buf.relay.clear()
  buf.history.clear()
  buf.objs.clear()
  buf.nests = []
  buf.plans.clear()
  buf.dirtyPlan.clear()
  buf.planInflight.clear()
  buf.planAtMs.clear()
  buf.unknownNest = false
  buf.unknownTask = false
  buf.refreshAtMs = 0
  buf.changed = true
}

const TOPIC = /^(nest|task|relay)\/([a-z0-9]+(?:-[a-z0-9]+)*)\/(status|stats)$/

function onData(m: DataMsg): void {
  if (!worldLive()) return
  const t = TOPIC.exec(m.topic)
  if (!t || !m.data || typeof m.data !== 'object') return
  const [, kind, id] = t
  if (kind === 'nest') {
    buf.nestStatus.set(id, m.data as NestStatus)
    if (!buf.nests.some((n) => n.id === id)) buf.unknownNest = true
  } else if (kind === 'task') {
    const s = m.data as TaskStatusMsg
    const prev = buf.status.get(id)
    buf.status.set(id, s)
    if (!buf.objs.has(id)) buf.unknownTask = true
    const plan = buf.plans.get(id)
    if (!plan || (s.revision ?? 0) > plan.revision || (prev && prev.revision !== s.revision)) buf.dirtyPlan.add(id)
  } else {
    const r = m.data as RelayStats
    buf.relay.set(id, r)
    const h = buf.history.get(id) ?? []
    const last = h[h.length - 1]
    if (!last || last.tMonitorS !== r.t_monitor_s) {
      h.push({ tMonitorS: r.t_monitor_s, coverage: r.coverage, gaps: r.gaps.count, maxGapS: r.gaps.max_s })
      if (h.length > RELAY_HISTORY) h.splice(0, h.length - RELAY_HISTORY)
      buf.history.set(id, h)
    }
  }
  buf.changed = true
}

const PLAN_EVENTS = new Set(['task.allocated', 'task.reallocated', 'task.state', 'relay.dispatch', 'relay.handover', 'task.created'])

function onEvents(batch: readonly RtEvent[]): void {
  if (!worldLive()) return
  for (const e of batch) {
    if (e.type === 'nest.launch' || e.type === 'nest.recover') {
      buf.changed = true
      continue
    }
    if (!PLAN_EVENTS.has(e.type)) continue
    const tid = typeof e.data?.tid === 'string' ? e.data.tid : null
    if (tid) buf.dirtyPlan.add(tid)
    if (e.type === 'task.created') buf.unknownTask = true
    buf.changed = true
  }
}

// ------------------------------------------------------------------------------------------------ summary (pure; tests)
function relaySummary(r: RelayStats | undefined): TaskRow['relay'] {
  if (!r) return null
  return { target: sanitizeText(String(r.target)), coverage: r.coverage, gaps: r.gaps.count, maxGapS: r.gaps.max_s, discovered: r.discovered,
    handovers: r.handovers.count, onStation: (r.on_station ?? []).map((o) => o.uav) }
}

/** status topics + task objects -> rows (sorted by tid: parents before the relays they spawned) */
export function summarizeTasks(status: ReadonlyMap<string, TaskStatusMsg>, objs: ReadonlyMap<string, TaskingTask>,
  relay: ReadonlyMap<string, RelayStats>): TaskRow[] {
  const ids = new Set<string>([...status.keys(), ...objs.keys()])
  const rows: TaskRow[] = []
  for (const tid of ids) {
    const s = status.get(tid)
    const o = objs.get(tid)
    const type = (s?.type ?? o?.type) as TaskType | undefined
    if (!type) continue
    const prog = s?.progress ?? o?.progress ?? {}
    rows.push({
      tid, type, label: sanitizeText(String(o?.label ?? '')).slice(0, 32), state: (s?.state ?? o?.state ?? 'PLANNING') as TaskState,
      revision: Math.max(s?.revision ?? 0, o?.revision ?? 0), parent: o?.parent ?? null, children: [...(s?.children ?? o?.children ?? [])],
      progressPct: Math.round((prog.pct ?? 0) * 10) / 10, coveragePct: prog.coverage_pct ?? null, nVehicles: s?.n_vehicles ?? o?.n_vehicles ?? 0,
      makespanS: s?.makespan_s ?? o?.makespan_s ?? null, items: [...(s?.items ?? o?.items ?? [])],
      warnings: (s?.warnings ?? []).map((w) => sanitizeText(String(w)).slice(0, 64)), params: (o?.params ?? {}) as Record<string, unknown>,
      relay: type === 'relay_watch' ? relaySummary(relay.get(tid)) : null,
    })
  }
  rows.sort((a, b) => cmpTid(a.tid, b.tid))
  return rows
}

/** t-2 < t-10 (numeric suffix), otherwise lexicographic */
export function cmpTid(a: string, b: string): number {
  const na = /^t-(\d+)$/.exec(a)
  const nb = /^t-(\d+)$/.exec(b)
  if (na && nb) return Number(na[1]) - Number(nb[1])
  return a < b ? -1 : a > b ? 1 : 0
}

function snapshotPatch(): Partial<TaskingState> {
  const nestStatus: Record<string, NestStatus> = {}
  for (const [k, v] of buf.nestStatus) nestStatus[k] = v
  const relay: Record<string, RelayStats> = {}
  for (const [k, v] of buf.relay) relay[k] = v
  const relayHistory: Record<string, RelaySample[]> = {}
  for (const [k, v] of buf.history) relayHistory[k] = v.slice()
  const plans: Record<string, TaskPlanEntry> = {}
  for (const [k, v] of buf.plans) plans[k] = v
  return { nests: buf.nests.slice(), nestStatus, tasks: summarizeTasks(buf.status, buf.objs, buf.relay), relay, relayHistory, plans }
}

// ------------------------------------------------------------------------------------------------ REST refreshes (sandbox)
function isSandbox(): boolean {
  return getEndpoint().kind === 'sandbox'
}

/** endpoint and world-scope epoch of a request: its answer is dropped when either changed meanwhile */
function stamp(): { ep: ReturnType<typeof getEndpoint>; epoch: number } {
  return { ep: getEndpoint(), epoch: worldScopeNow().epoch }
}
const outdated = (s: { ep: ReturnType<typeof getEndpoint>; epoch: number }): boolean => getEndpoint() !== s.ep || worldScopeNow().epoch !== s.epoch

function noteError(e: unknown): void {
  const s = taskingStore.getState()
  const err = e instanceof ApiError ? { code: e.reason ?? e.status, message: e.message, detail: e.detail } : { code: 0, message: String(e), detail: null }
  taskingStore.setState({ lastError: err, version: s.version + 1 })
}

export async function refreshNests(): Promise<void> {
  if (!isSandbox() || !worldLive()) return
  const st = stamp()
  try {
    const r = await port.get<{ items: Nest[] }>('/nests')
    if (outdated(st)) return
    buf.nests = [...(r.items ?? [])].sort((a, b) => cmpTid(String(a.id ?? ''), String(b.id ?? '')))
    buf.unknownNest = false
    buf.changed = true
  } catch (e) {
    noteError(e)
  }
}

export async function refreshTasks(): Promise<void> {
  if (!isSandbox() || !worldLive()) return
  const st = stamp()
  try {
    const r = await port.get<{ items: TaskingTask[] }>('/tasks')
    if (outdated(st)) return
    buf.objs.clear()
    for (const o of r.items ?? []) buf.objs.set(o.tid, o)
    buf.unknownTask = false
    buf.changed = true
  } catch (e) {
    noteError(e)
  }
}

/** SB-54 include=plan of one task (sandbox; no-op on the shared show) */
export async function refreshPlan(tid: string): Promise<void> {
  if (!isSandbox() || !worldLive() || buf.planInflight.has(tid)) return
  const st = stamp()
  buf.planInflight.add(tid)
  buf.planAtMs.set(tid, Date.now())
  buf.dirtyPlan.delete(tid)
  try {
    const o = await port.get<TaskingTask>(`/tasks/${encodeURIComponent(tid)}?include=plan`)
    if (outdated(st)) return
    buf.objs.set(tid, o)
    if (o.plan) buf.plans.set(tid, { revision: o.plan.revision, geometry: o.plan.geometry ?? {}, assignments: o.plan.assignments ?? [] })
    buf.changed = true
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      buf.plans.delete(tid)
      buf.changed = true
    } else noteError(e)
  } finally {
    buf.planInflight.delete(tid)
  }
}

function scheduleRest(nowMs: number): void {
  if (!isSandbox() || !worldLive() || taskingStore.getState().stale) return
  if (buf.unknownNest || nowMs - buf.refreshAtMs > REFRESH_MS) void refreshNests()
  if (buf.unknownTask || nowMs - buf.refreshAtMs > REFRESH_MS) void refreshTasks()
  if (nowMs - buf.refreshAtMs > REFRESH_MS) buf.refreshAtMs = nowMs
  for (const tid of buf.dirtyPlan) {
    const st = buf.status.get(tid)?.state
    if (st === 'PLANNING') continue
    if (nowMs - (buf.planAtMs.get(tid) ?? 0) < PLAN_MIN_GAP_MS) continue
    void refreshPlan(tid)
  }
}

// ------------------------------------------------------------------------------------------------ tick
let lastKey = ''

function tick(_ctx: FrameCtx | null, rt: RtClient | null): void {
  const nowMs = Date.now()
  if (rt) {
    const role = rt.serverInfo?.role ?? 'viewer'
    if (role !== buf.role) {
      buf.role = role
      buf.changed = true
    }
    if (rt.status !== buf.conn) {
      buf.conn = rt.status
      buf.changed = true
    }
  }
  scheduleRest(nowMs)
  if (!buf.changed) return
  buf.changed = false
  const stale = buf.conn === 'RECONNECTING' || buf.conn === 'FATAL' || buf.conn === 'CLOSED'
  const patch: Partial<TaskingState> = { ...snapshotPatch(), stale, canEdit: !stale && (buf.role === 'operator' || buf.role === 'admin'),
    mode: isSandbox() ? 'sandbox' : 'shared' }
  const key = JSON.stringify([patch.nests, patch.nestStatus, patch.tasks, patch.relay, Object.keys(patch.plans ?? {}).map((k) => [k, patch.plans?.[k].revision]),
    patch.stale, patch.canEdit, patch.mode])
  if (key === lastKey) return
  lastKey = key
  taskingStore.setState({ ...patch, version: taskingStore.getState().version + 1 })
}

// ------------------------------------------------------------------------------------------------ feed
let refs = 0
let offs: (() => void)[] = []
let rtGetter: () => RtClient | null = rtClient

/** forget everything (endpoint change, tests) */
export function resetTasking(): void {
  resetBuffers()
  lastKey = ''
  taskingStore.setState({ ...blank(), version: taskingStore.getState().version + 1 })
}

onWorldReset('tasking', () => resetTasking())

/** reference-counted feed (useTasking and the viewport layer attach it); returns the detach function */
export function attachTaskingFeed(getRt: () => RtClient | null = rtClient): () => void {
  refs++
  if (refs === 1) {
    rtGetter = getRt
    const rt = getRt()
    offs = [
      loop.register('overlay', 'm20.tasking-store', (ctx) => tick(ctx, getRt()), { fps: TASKING_STORE_HZ, order: 23, tiers: ['A', 'B'] }),
      loop.register('overlay', 'm20.tasking-store.s', (ctx) => void (uiTickDue(ctx) && tick(ctx, getRt())), { order: 23, tiers: ['S'] }),
      onEndpointChange(() => resetTasking()),
    ]
    if (rt) {
      offs.push(rt.onData(onData), rt.onEvents(onEvents), rt.subscribe('nest/*/status', { rate: 1 }), rt.subscribe('task/*/status', { rate: 1 }),
        rt.subscribe('relay/*/stats', { rate: 1 }))
    }
    buf.refreshAtMs = 0
    buf.changed = true
  }
  let done = false
  return () => {
    if (done) return
    done = true
    refs--
    if (refs === 0) {
      for (const off of offs) off()
      offs = []
      lastKey = ''
      rtGetter = rtClient
    }
  }
}

/** test helper: one feed tick without the frame loop */
export function taskingFeedTick(rt: RtClient | null): void {
  tick(null, rt)
}

// ------------------------------------------------------------------------------------------------ actions
export interface TaskingOutcome<T = unknown> {
  ok: boolean
  code: number
  /** field-level detail of 110, the why and remedies of 580 to 585, violations of 584 */
  detail?: Record<string, unknown>
  data?: T
  warnings?: string[]
}

function fail<T>(e: unknown): TaskingOutcome<T> {
  if (e instanceof ApiError) {
    const d = e.detail && typeof e.detail === 'object' ? (e.detail as Record<string, unknown>) : undefined
    return { ok: false, code: e.reason ?? e.status, detail: d }
  }
  return { ok: false, code: 0, detail: { error: String(e) } }
}

function fromCall<T>(r: CallResult): TaskingOutcome<T> {
  const ok = r.status === 'succeeded' || r.status === 'accepted' || r.status === 'running'
  const data = (r.data ?? {}) as { result?: T }
  return { ok, code: r.code, data: (data.result ?? (r.data as T)) ?? undefined,
    detail: r.detail && typeof r.detail === 'object' ? (r.detail as Record<string, unknown>) : undefined, warnings: r.warnings }
}

async function viaRest<T>(fn: () => Promise<T>, after?: () => void): Promise<TaskingOutcome<T>> {
  try {
    const data = await fn()
    after?.()
    return { ok: true, code: 0, data }
  } catch (e) {
    return fail<T>(e)
  }
}

async function viaCall<T>(service: string, args: object): Promise<TaskingOutcome<T>> {
  const rt = rtGetter()
  if (!rt) return { ok: false, code: 211 }
  const r = await rt.call(service, args).result
  return fromCall<T>(r)
}

function guard<T>(): TaskingOutcome<T> | null {
  const s = taskingStore.getState()
  if (s.stale) return { ok: false, code: 211, detail: { why: 'stale' } }
  return null
}

let idemSeq = 0
const idem = (prefix: string): Record<string, string> => ({ 'Idempotency-Key': `${prefix}-${Date.now().toString(36)}-${(idemSeq++).toString(36)}` })

export const taskingActions = {
  // -------------------------------------------------------------- nests (SB-48, SB-50, SB-51)
  /** add a nest; `inventory[{model_id, count}]` places vehicles at once (reply vehicle_ids); 505 at 4, 525 incompatible */
  addNest(nest: Nest): Promise<TaskingOutcome<Nest>> {
    const g = guard<Nest>()
    if (g) return Promise.resolve(g)
    if (!isSandbox()) return viaCall<Nest>('nest/add', { nest })
    return viaRest(() => port.post<Nest>('/nests', nest, idem('nest')), () => void refreshNests())
  },
  updateNest(id: string, patch: Partial<Nest>): Promise<TaskingOutcome<Nest>> {
    const g = guard<Nest>()
    if (g) return Promise.resolve(g)
    if (!isSandbox()) return viaCall<Nest>('nest/update', { id, patch })
    return viaRest(() => port.patch<Nest>(`/nests/${encodeURIComponent(id)}`, patch), () => void refreshNests())
  },
  removeNest(id: string): Promise<TaskingOutcome<number>> {
    const g = guard<number>()
    if (g) return Promise.resolve(g)
    if (!isSandbox()) return viaCall<number>('nest/remove', { id })
    return viaRest(() => port.del(`/nests/${encodeURIComponent(id)}`), () => {
      buf.nests = buf.nests.filter((n) => n.id !== id)
      buf.nestStatus.delete(id)
      buf.changed = true
    })
  },
  // -------------------------------------------------------------- tasks (SB-53, SB-55, SB-68)
  /** dry-run (viewers too): the result becomes `preview` (drawn by the layer); 202 polls SB-68 every second (<= 60 s) */
  async preview(spec: TaskSpecBody): Promise<PreviewEntry> {
    const entry: PreviewEntry = { spec, state: 'pending', previewId: null, preview: null, code: 0, detail: null, tWallMs: Date.now() }
    const epoch = worldScopeNow().epoch
    const set = (e: PreviewEntry): void => {
      const s = taskingStore.getState()
      // a preview of the previous world never lands after a world or source switch
      if (worldScopeNow().epoch !== epoch || (s.preview && s.preview.tWallMs > e.tWallMs)) return
      taskingStore.setState({ preview: { ...e }, version: s.version + 1 })
    }
    set(entry)
    if (!isSandbox()) {
      const e = { ...entry, state: 'error' as const, code: 305, detail: { why: 'sandbox_only' } }
      set(e)
      return e
    }
    void loadPreviewKeepouts()
    try {
      let r = await port.post<TaskingTaskPreview & { state?: string }>('/tasks?dry_run=true', spec)
      const t0 = Date.now()
      while (r.state === 'pending' && Date.now() - t0 < 60_000) {
        entry.previewId = r.preview_id
        await sleep(PREVIEW_POLL_MS)
        r = await port.get<TaskingTaskPreview & { state?: string }>(`/tasks/previews/${encodeURIComponent(r.preview_id)}`)
      }
      if (r.state === 'pending') Object.assign(entry, { state: 'error', code: 125, detail: { why: 'timeout' } })
      else Object.assign(entry, { state: 'done', previewId: r.preview_id, preview: r })
    } catch (e) {
      const f = fail(e)
      Object.assign(entry, { state: f.code === 0 ? 'error' : 'infeasible', code: f.code, detail: f.detail ?? null,
        previewId: (f.detail?.preview_id as string | undefined) ?? entry.previewId })
    }
    set(entry)
    return entry
  },
  clearPreview(): void {
    const s = taskingStore.getState()
    if (s.preview || s.previewKeepouts.length) taskingStore.setState({ preview: null, previewKeepouts: [], version: s.version + 1 })
  },
  /** create (reuses the active preview's plan when the spec is the same, 17 §17.6.7); returns the task object */
  async create(spec: TaskSpecBody): Promise<TaskingOutcome<TaskingTask>> {
    const g = guard<TaskingTask>()
    if (g) return g
    const pv = taskingStore.getState().preview
    const body: TaskSpecBody = { ...spec }
    if (body.preview_id === undefined && pv?.state === 'done' && pv.previewId && sameSpec(pv.spec, spec)) body.preview_id = pv.previewId
    if (!isSandbox()) return viaCall<TaskingTask>('task/create', body)
    const out = await viaRest(() => port.post<TaskingTask>('/tasks', body, idem('task')), () => void refreshTasks())
    if (out.ok && out.data?.tid) {
      buf.objs.set(out.data.tid, out.data)
      buf.dirtyPlan.add(out.data.tid)
      buf.changed = true
      taskingActions.select(out.data.tid)
    }
    return out
  },
  control(tid: string, op: 'start' | 'pause' | 'resume' | 'cancel'): Promise<TaskingOutcome<{ tid: string; state: TaskState }>> {
    const g = guard<{ tid: string; state: TaskState }>()
    if (g) return Promise.resolve(g)
    if (!isSandbox()) return viaCall(`task/${tid}/${op}`, {})
    return viaRest(() => port.post<{ tid: string; state: TaskState }>(`/tasks/${encodeURIComponent(tid)}:${op}`, {}), () => {
      buf.dirtyPlan.add(tid)
      buf.changed = true
    })
  },
  /** SB-56 (24 to 168 h) */
  sustainability(tid: string, horizonH = 168): Promise<TaskingOutcome<Sustainability>> {
    if (!isSandbox()) return Promise.resolve({ ok: false, code: 305, detail: { why: 'sandbox_only' } })
    const h = Math.min(168, Math.max(24, Math.round(horizonH)))
    return viaRest(() => port.get<Sustainability>(`/tasks/${encodeURIComponent(tid)}/sustainability?horizon_h=${h}`))
  },
  // -------------------------------------------------------------- external allocator (SB-57, SB-58)
  snapshot(): Promise<TaskingOutcome<TaskingSnapshot>> {
    if (!isSandbox()) return Promise.resolve({ ok: false, code: 305, detail: { why: 'sandbox_only' } })
    return viaRest(() => port.get<TaskingSnapshot>('/tasking/snapshot'))
  },
  submitPlan(basedOnRevision: number, assignments: { wid: string; uav: string; order?: number }[]): Promise<TaskingOutcome<PlanReply>> {
    const g = guard<PlanReply>()
    if (g) return Promise.resolve(g)
    const body = { based_on_revision: basedOnRevision, allocator: 'external', assignments }
    if (!isSandbox()) return viaCall<PlanReply>('tasking/plan', body)
    return viaRest(() => port.post<PlanReply>('/tasking/plans', body, idem('plan')))
  },
  // -------------------------------------------------------------- quick actions (tablet command remote, phone)
  /** application scenario one in one step: area_guard over a polygon (on_detect relay, defaults of 17 §17.6.7) */
  guardArea(polygon: readonly (readonly [number, number])[], o: { targetClass?: string; levelReq?: 'D' | 'R' | 'I'; revisitS?: number; label?: string } = {}) {
    return taskingActions.create({ type: 'area_guard', label: o.label ?? null, params: { polygon: polygon.map((p) => [p[0], p[1]]),
      target_class: o.targetClass ?? 'person', level_req: o.levelReq ?? 'R', revisit_s: o.revisitS ?? 300 } })
  },
  /** watch a point until canceled (multirotor hover, fixed wing loiter) */
  watchPoint(point: readonly [number, number] | Vec3, o: { altM?: number; dwellS?: number | null; label?: string; look?: { mode: 'target'; target: string } } = {}) {
    return taskingActions.create({ type: 'point_watch', label: o.label ?? null, params: { point: [point[0], point[1], point[2] ?? 0], alt_m: o.altM ?? 60,
      dwell_s: o.dwellS === undefined ? null : o.dwellS, ...(o.look ? { look: o.look } : {}) } })
  },
  /** 7x24 relay watch of a detected target (precheck 581 with suggestions when no station is feasible) */
  relayTarget(target: string, levelReq?: 'D' | 'R' | 'I') {
    return taskingActions.create({ type: 'relay_watch', params: { target, ...(levelReq ? { level_req: levelReq } : {}) } })
  },
  select(tid: string | null): void {
    const s = taskingStore.getState()
    if (s.selected === tid) return
    taskingStore.setState({ selected: tid, version: s.version + 1 })
    if (tid && !buf.plans.has(tid)) buf.dirtyPlan.add(tid)
  },
  clearError(): void {
    const s = taskingStore.getState()
    if (s.lastError) taskingStore.setState({ lastError: null, version: s.version + 1 })
  },
}

async function loadPreviewKeepouts(): Promise<void> {
  const st = stamp()
  try {
    const snap = await port.get<TaskingSnapshot>('/tasking/snapshot')
    if (outdated(st)) return
    const ko: KeepoutCyl[] = []
    for (const k of snap.keepouts ?? []) {
      const kk = k as { id?: string; center_enu_m?: number[]; r_m?: number }
      if (!kk.center_enu_m || typeof kk.r_m !== 'number') continue
      ko.push({ id: String(kk.id ?? ''), x: kk.center_enu_m[0], y: kk.center_enu_m[1], r: kk.r_m })
    }
    const s = taskingStore.getState()
    taskingStore.setState({ previewKeepouts: ko, version: s.version + 1 })
  } catch {
    // keepouts are decoration of the preview: a failure leaves the band out
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

function sameSpec(a: TaskSpecBody, b: TaskSpecBody): boolean {
  const norm = (x: TaskSpecBody) => JSON.stringify({ type: x.type, params: x.params, common: x.common ?? {} })
  return norm(a) === norm(b)
}

// ------------------------------------------------------------------------------------------------ selectors
/** plan geometry of a task (null until fetched; relay stations included); the active preview when tid is its preview id */
export function taskGeometry(s: TaskingState, tid: string): TaskGeometry | null {
  if (s.preview?.previewId === tid && s.preview.preview?.geometry) return s.preview.preview.geometry as TaskGeometry
  return s.plans[tid]?.geometry ?? null
}

/** launch and recover queues of a nest (status topic) */
export function nestQueue(s: TaskingState, id: string): { launch: NonNullable<NestStatus['launch_queue']>; recover: NonNullable<NestStatus['recover_queue']> } {
  const st = s.nestStatus[id]
  return { launch: [...(st?.launch_queue ?? [])], recover: [...(st?.recover_queue ?? [])] }
}

/** ready packs of a nest and whether a dispatch is waiting for a battery ("dispatch delayed (battery)") */
export function nestPacks(s: TaskingState, id: string): { ready: number; charging: number; queued: number; batteryDelay: boolean } {
  const st = s.nestStatus[id]
  const packs = st?.packs ?? []
  return { ready: packs.filter((p) => p.state === 'ready').length, charging: packs.filter((p) => p.state === 'charging').length,
    queued: packs.filter((p) => p.state === 'queued').length, batteryDelay: (st?.launch_queue ?? []).some((q) => q.delay_reason === 'battery') }
}

/** vehicle -> task assignment (wid, kind, tid) from the plans and the status items (the layer's assignment shading) */
export function assignmentOf(s: TaskingState): Map<string, { tid: string; wid: string; kind: string }> {
  const out = new Map<string, { tid: string; wid: string; kind: string }>()
  for (const t of s.tasks) {
    if (TERMINAL.includes(t.state)) continue
    for (const it of t.items) if (it.uav && (it.state === 'ASSIGNED' || it.state === 'ACTIVE')) out.set(it.uav, { tid: t.tid, wid: it.wid, kind: it.kind })
  }
  return out
}

/** compact summary for the phone and tablet views (one line per active task, the relays first) */
export interface TaskingSummary {
  active: number
  running: number
  relays: { tid: string; target: string; coverage: number; gaps: number; onStation: string[] }[]
  airborne: number
  readyPacks: number
  warnings: number
}
export function taskingSummary(s: TaskingState): TaskingSummary {
  let active = 0
  let running = 0
  let warnings = 0
  const relays: TaskingSummary['relays'] = []
  for (const t of s.tasks) {
    if (!ACTIVE.includes(t.state)) continue
    active++
    if (t.state === 'RUNNING') running++
    warnings += t.warnings.length
    if (t.relay) relays.push({ tid: t.tid, target: t.relay.target, coverage: t.relay.coverage, gaps: t.relay.gaps, onStation: t.relay.onStation })
  }
  let airborne = 0
  let readyPacks = 0
  for (const st of Object.values(s.nestStatus)) {
    airborne += (st.aircraft ?? []).filter((a) => a.state === 'airborne' || a.state === 'standby').length
    readyPacks += (st.packs ?? []).filter((p) => p.state === 'ready').length
  }
  return { active, running, relays, airborne, readyPacks, warnings }
}

/** i18n key of an infeasible reason (M20-NFR-006: `tasking.why.<why>`, falling back to the reason code) */
export function whyKey(code: number, why: unknown): string {
  return typeof why === 'string' && /^[a-z_]+$/.test(why) ? `tasking.why.${why}` : `reason.${code}`
}

/** React hook: selector over the tasking store; mounting it attaches the feed */
export function useTasking<T>(sel: (s: TaskingState) => T): T {
  useEffect(() => attachTaskingFeed(), [])
  return useStore(taskingStore, sel)
}

// ------------------------------------------------------------------------------------------------ layer lines (pure; the layer and tests)
/**
 * Line lists of the tasking layer (M20 PRD §8 item 2), written into two sinks (the layer passes its ThinLineBatch and
 * WideLineBatch; tests pass recorders): nests (square with slot ticks), sub-area outlines, strips and legs (<= 512
 * segments together, the layer cap of 17 §17.6.7), fill crosses, stations (primary: solid ring with a plumb line; handover:
 * dashed ring; fixed-wing loiter circle), and during a dry-run the preview geometry with the keepout band (dashed circles).
 * Palette indices are those of SCENE.glyphPalette (0 g50, 2 g500, 3 g400, 4 g300, 5 g200); 1 (r500) belongs to the
 * RedArbiter (AWR-04 §10.7) and is never written. Assignment shading without colour: one of six styles (g200, g300, g400 x
 * solid, dashed) per vehicle in id order; unassigned and preview geometry g400 dashed; the selected task goes to the wide
 * sink in g50. Governor levels: 0 full, 1 other tasks' legs off, 2 only the selected task (stations of the others),
 * 3 nests and stations only.
 */
export const LINE_PAL = { G50: 0, G500: 2, G400: 3, G300: 4, G200: 5 } as const
export const LINE_STYLES: readonly { pal: number; dashed: boolean }[] = [
  { pal: LINE_PAL.G200, dashed: false }, { pal: LINE_PAL.G300, dashed: false }, { pal: LINE_PAL.G400, dashed: false },
  { pal: LINE_PAL.G200, dashed: true }, { pal: LINE_PAL.G300, dashed: true }, { pal: LINE_PAL.G400, dashed: true },
]
export const LINE_UNASSIGNED = { pal: LINE_PAL.G400, dashed: true } as const
export const LINE_CAPS = { thin: 4096, wide: 512, legs: 512, rings: 16, keepoutRing: 32, nests: 4, stations: 32, fills: 256 } as const
const STATION_R_M = 6
const NEST_HALF_M = 4
const FILL_HALF_M = 2.5
const LIFT_M = 1

export interface ThinSink { readonly n: number; seg(ax: number, ay: number, az: number, bx: number, by: number, bz: number, pal: number, dashed: boolean, alpha: number, d0?: number): boolean }
export interface WideSink { readonly n: number; seg(ax: number, ay: number, az: number, bx: number, by: number, bz: number, pal: number, d0?: number): boolean }
export interface LineStats { segments: number; wide: number; stations: number; nests: number; fills: number; strips: number; keepouts: number; tasks: number; clipped: boolean }
type LineStyle = { pal: number; dashed: boolean }

export function buildTaskingLines(s: TaskingState, ground: (x: number, y: number) => number, level: number, thin: ThinSink, wide: WideSink): LineStats {
  const st: LineStats = { segments: 0, wide: 0, stations: 0, nests: 0, fills: 0, strips: 0, keepouts: 0, tasks: 0, clipped: false }
  let legBudget: number = LINE_CAPS.legs
  const ring = (x: number, y: number, z: number, r: number, k: number, pal: number, dashed: boolean, toWide = false): void => {
    let d = 0
    for (let i = 0; i < k; i++) {
      const a0 = (2 * Math.PI * i) / k
      const a1 = (2 * Math.PI * (i + 1)) / k
      const ax = x + r * Math.cos(a0), ay = y + r * Math.sin(a0), bx = x + r * Math.cos(a1), by = y + r * Math.sin(a1)
      if (toWide) wide.seg(ax, ay, z, bx, by, z, LINE_PAL.G50)
      else thin.seg(ax, ay, z, bx, by, z, pal, dashed, 0.9, d)
      d += Math.hypot(bx - ax, by - ay)
    }
  }
  // assignment styles: vehicles of the shown plans in id order
  const ids = new Set<string>()
  for (const t of s.tasks) {
    const p = s.plans[t.tid]
    if (!p) continue
    for (const a of p.assignments) ids.add(a.uav)
    for (const l of p.geometry.legs ?? []) if (l.uav && l.uav !== 'unassigned') ids.add(l.uav)
  }
  const idx = new Map([...ids].sort().map((u, i) => [u, i]))
  const styleOf = (uav: string | null | undefined): LineStyle => {
    const i = uav ? idx.get(uav) : undefined
    return i === undefined ? LINE_UNASSIGNED : LINE_STYLES[i % LINE_STYLES.length]
  }
  const stations = (g: TaskGeometry, toWide: boolean): void => {
    for (const p of (g.stations ?? []).slice(0, LINE_CAPS.stations)) {
      const [x, y, z] = p.pos
      const hand = p.role === 'handover'
      ring(x, y, z, STATION_R_M, LINE_CAPS.rings, hand ? LINE_PAL.G300 : p.uav ? styleOf(p.uav).pal : LINE_PAL.G400, hand)
      if (!hand) thin.seg(x, y, z, x, y, ground(x, y) + LIFT_M, LINE_PAL.G400, true, 0.6)
      if (p.loiter_radius_m && p.loiter_radius_m > STATION_R_M) ring(x, y, z, p.loiter_radius_m, LINE_CAPS.keepoutRing, LINE_PAL.G300, true)
      if (toWide) ring(x, y, z, STATION_R_M + 1.5, LINE_CAPS.rings, LINE_PAL.G50, false, true)
      st.stations++
    }
  }
  const geometry = (g: TaskGeometry, style: (uav: string | null | undefined) => LineStyle, legs: boolean, toWide: boolean): void => {
    for (const poly of g.subregions ?? []) {
      for (let i = 0; i < poly.length; i++) {
        const a = poly[i]
        const b = poly[(i + 1) % poly.length]
        thin.seg(a[0], a[1], ground(a[0], a[1]) + LIFT_M, b[0], b[1], ground(b[0], b[1]) + LIFT_M, LINE_PAL.G400, false, 0.7)
      }
    }
    for (const p of g.strips ?? []) {
      if (legBudget-- <= 0) {
        st.clipped = true
        break
      }
      const sty = style(p.uav)
      thin.seg(p.p0[0], p.p0[1], p.p0[2], p.p1[0], p.p1[1], p.p1[2], sty.pal, sty.dashed, 0.9)
      if (toWide) wide.seg(p.p0[0], p.p0[1], p.p0[2], p.p1[0], p.p1[1], p.p1[2], LINE_PAL.G50)
      st.strips++
    }
    if (legs) {
      for (const l of g.legs ?? []) {
        const sty = style(l.uav)
        const P = l.polyline
        let d = 0
        for (let i = 0; i + 1 < P.length; i++) {
          if (legBudget-- <= 0) {
            st.clipped = true
            break
          }
          const a = P[i]
          const b = P[i + 1]
          thin.seg(a[0], a[1], a[2], b[0], b[1], b[2], sty.pal, sty.dashed || l.kind === 'transit' || l.kind === 'return', 0.8, d)
          if (toWide) wide.seg(a[0], a[1], a[2], b[0], b[1], b[2], LINE_PAL.G50, d)
          d += Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2])
        }
      }
    }
    for (const f of (g.fill_waypoints ?? []).slice(0, LINE_CAPS.fills)) {
      const [x, y, z] = f.pos
      thin.seg(x - FILL_HALF_M, y, z, x + FILL_HALF_M, y, z, LINE_PAL.G200, false, 0.9)
      thin.seg(x, y - FILL_HALF_M, z, x, y + FILL_HALF_M, z, LINE_PAL.G200, false, 0.9)
      st.fills++
    }
    stations(g, toWide)
  }
  for (const n of s.nests.slice(0, LINE_CAPS.nests)) {
    const x = Number(n.pos[0] ?? 0)
    const y = Number(n.pos[1] ?? 0)
    const z = ((n.pos[2] ?? null) === null ? ground(x, y) : Number(n.pos[2])) + LIFT_M
    const h = NEST_HALF_M
    const c: [number, number][] = [[x - h, y - h], [x + h, y - h], [x + h, y + h], [x - h, y + h]]
    for (let i = 0; i < 4; i++) thin.seg(c[i][0], c[i][1], z, c[(i + 1) % 4][0], c[(i + 1) % 4][1], z, LINE_PAL.G200, false, 1)
    const k = Math.max(1, Math.min(12, n.slots ?? 6))
    for (let i = 0; i < k; i++) {
      const tx = x - h + ((i + 0.5) * 2 * h) / k
      thin.seg(tx, y - h, z, tx, y - h + 1.2, z, LINE_PAL.G300, false, 1)
    }
    st.nests++
  }
  for (const t of s.tasks) {
    if (!ACTIVE.includes(t.state) && t.tid !== s.selected) continue
    const g = taskGeometry(s, t.tid)
    if (!g) continue
    const sel = t.tid === s.selected
    if ((level >= 2 && !sel) || level >= 3) {
      stations(g, false)
      continue
    }
    st.tasks++
    geometry(g, styleOf, level === 0 || sel, sel)
  }
  if (s.preview?.state === 'done' && s.preview.preview?.geometry && level < 3) {
    geometry(s.preview.preview.geometry as TaskGeometry, () => LINE_UNASSIGNED, true, false)
    for (const k of s.previewKeepouts.slice(0, 32)) {
      ring(k.x, k.y, ground(k.x, k.y) + LIFT_M, k.r, LINE_CAPS.keepoutRing, LINE_PAL.G300, true)
      st.keepouts++
    }
  }
  st.segments = thin.n
  st.wide = wide.n
  st.clipped = st.clipped || thin.n >= LINE_CAPS.thin || wide.n >= LINE_CAPS.wide
  return st
}
