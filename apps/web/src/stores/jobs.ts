// Jobs store (AWR-03 §4.3: owner M03; D1-ext; AWR-14 §5.7; AWR-17 §4.3.10 R38-R42, R63, R64, §6.12; M01 §7.1, §7.2, §8.2).
// Rows of the Reconstruction Jobs page: loaded from GET /api/jobs (R39) and kept current by the `job.state` (reliable,
// every change) and `job.progress` (250 ms per job on the server) events of the page RtClient. Events are folded into a
// pending map and written to the store at most every INPUT-free 250 ms (<= 4 Hz, D1-AC-22 "progress <= 4 Hz"); progress
// is monotonic per job and attempt (a late progress never moves a row back). Actions: submit a Mock reconstruction
// (R38, Idempotency-Key), cancel (R41), retry a resumable failure (R42), the log tail (R64) and the engine list (R63).
// When the job service is missing (404 route, 503 213 JOB_WORKER_UNAVAILABLE) the store says so instead of pretending
// an empty queue. FX-WEB2 replaced the M15-S skeleton (JobState now follows rt/enums.json JobState.recon and
// JobState.world_build; M01-to-M03 item 8).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { ApiError, apiGet, apiPost } from '@/net/api'
import type { RtClient, RtEvent } from '@/net/rt'

/** JobState.recon and JobState.world_build (packages/contracts/rt/enums.json) */
export const RECON_STAGES = ['PREPARING', 'SEGMENTING', 'INFERRING', 'FUSING', 'GEOREFERENCING', 'TILING', 'PACKAGING'] as const
export const BUILD_STAGES = ['INGESTING', 'TILING', 'DERIVING', 'VALIDATING', 'PUBLISHING'] as const
export const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED'])
export type JobState = 'QUEUED' | (typeof RECON_STAGES)[number] | (typeof BUILD_STAGES)[number] | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'

export interface JobAlignment { method: string | null; status: string | null; inlierRatio: number | null; rmseM: number | null; needsReview: boolean }
export interface JobError { code: number; name: string | null; message: string | null; stage: string | null; resumable: boolean }
export interface JobRow {
  jobId: string
  kind: string
  state: string
  stage: string | null
  /** 0-100 */
  progressPct: number
  submittedBy: string | null
  createdMs: number
  updatedMs: number
  targetWorldId: string | null
  sourceWorldId: string | null
  engine: string | null
  sessionId: string | null
  /** relative | gnss | rtk | lidar (ScaleStatus subset); null until GEOREFERENCING finished */
  scaleStatus: string | null
  alignment: JobAlignment | null
  framesDone: number | null
  framesTotal: number | null
  fps: number | null
  etaS: number | null
  pausedReason: string | null
  attempt: number
  error: JobError | null
  resumable: boolean
  outputWorldId: string | null
  stageDurationsS: Readonly<Record<string, number>>
}
export type JobsStatus = 'idle' | 'loading' | 'ready' | 'unavailable' | 'error'
export interface JobsState {
  jobs: readonly JobRow[]
  version: number
  status: JobsStatus
  /** reason code of the last failed load (213 worker unavailable, 404 no route) */
  code: number | null
  /** store writes caused by events (tests: the <= 4 Hz flush) */
  flushes: number
}

export const jobsStore = createAwrStore<JobsState>('jobs', () => ({ jobs: [], version: 0, status: 'idle', code: null, flushes: 0 }))

export function useJobs<T>(sel: (s: JobsState) => T): T {
  return useStore(jobsStore, sel)
}

// ------------------------------------------------------------------ mapping
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v !== '' && Number.isFinite(Number(v)) ? Number(v) : null)
const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const obj = (v: unknown): Record<string, unknown> | null => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null)
const nsToMs = (v: unknown): number => {
  const n = num(v)
  return n === null ? Number.NaN : n / 1e6
}

function errorOf(v: unknown, resumable: unknown): JobError | null {
  const e = obj(v)
  if (!e) return null
  const code = num(e.code)
  return {
    code: code ?? 0, name: str(e.name), message: str(e.message) ?? str(e.detail), stage: str(e.stage),
    resumable: e.resumable === true || resumable === true || resumable === 1,
  }
}

function alignmentOf(v: unknown): JobAlignment | null {
  const a = obj(v)
  if (!a) return null
  return {
    method: str(a.method), status: str(a.status), inlierRatio: num(a.inlier_ratio ?? a.inliers_ratio), rmseM: num(a.rmse_m),
    needsReview: a.needs_review === true,
  }
}

/** one R39/R40 item (snake_case wire) -> JobRow */
export function jobFromWire(x: Record<string, unknown>): JobRow {
  const r = obj(x.recon) ?? {}
  const frames = obj(r.frames) ?? {}
  const out = obj(x.output)
  const err = errorOf(x.error, x.resumable)
  return {
    jobId: str(x.job_id) ?? '', kind: str(x.kind) ?? 'recon', state: str(x.state) ?? 'QUEUED', stage: str(x.stage),
    progressPct: Math.max(0, Math.min(100, num(x.progress_pct) ?? 0)), submittedBy: str(x.submitted_by),
    createdMs: nsToMs(x.created_unix_ns), updatedMs: nsToMs(x.updated_unix_ns ?? x.created_unix_ns),
    targetWorldId: str(x.target_world_id) ?? str(r.target_world_id), sourceWorldId: str(r.source_world_id), engine: str(r.engine),
    sessionId: str(r.session_id), scaleStatus: str(x.scale_status) ?? str(r.scale_status), alignment: alignmentOf(r.alignment),
    framesDone: num(frames.done), framesTotal: num(frames.total), fps: null, etaS: num(x.eta_s), pausedReason: str(r.paused_reason),
    attempt: num(x.attempt) ?? 0, error: err, resumable: err?.resumable ?? (x.resumable === true || x.resumable === 1),
    outputWorldId: str(out?.world_id) ?? (str(x.state) === 'SUCCEEDED' ? str(x.target_world_id) : null),
    stageDurationsS: (obj(r.stage_durations_s) as Record<string, number> | null) ?? {},
  }
}

/** fold one job event into a row (a new row when the job is unknown); returns the updated row */
export function applyJobEvent(prev: JobRow | undefined, type: string, d: Record<string, unknown>, tWallMs: number): JobRow {
  const base: JobRow = prev ?? jobFromWire({ job_id: d.job_id, kind: d.kind, state: d.state ?? 'QUEUED', target_world_id: d.target_world_id })
  const row: JobRow = { ...base }
  const attempt = num(d.attempt)
  const newAttempt = attempt !== null && attempt > row.attempt
  if (attempt !== null) row.attempt = Math.max(row.attempt, attempt)
  if (str(d.state)) row.state = String(d.state)
  if (d.stage !== undefined) row.stage = str(d.stage)
  const pct = num(d.progress_pct)
  // progress never moves back within one attempt (M01 §6.7.3: progress_pct is monotonic; a retry starts over)
  if (pct !== null) row.progressPct = newAttempt ? Math.min(100, Math.max(0, pct)) : Math.min(100, Math.max(row.progressPct, pct))
  if (d.eta_s !== undefined) row.etaS = num(d.eta_s)
  if (type === 'job.progress') {
    if (d.frames_done !== undefined) row.framesDone = num(d.frames_done)
    if (d.frames_total !== undefined) row.framesTotal = num(d.frames_total)
    if (d.fps !== undefined) row.fps = num(d.fps)
    row.pausedReason = str(d.paused_reason)
  } else {
    const out = obj(d.output)
    if (out) {
      row.outputWorldId = str(out.world_id) ?? row.outputWorldId
      row.scaleStatus = str(out.scale_status) ?? row.scaleStatus
      row.sessionId = str(out.recon_session) ?? row.sessionId
    }
    const err = errorOf(d.error, undefined)
    if (err) {
      row.error = err
      row.resumable = err.resumable
    } else if (row.state !== 'FAILED') row.error = null
    const sd = obj(d.stage_durations_s)
    if (sd) row.stageDurationsS = sd as Record<string, number>
    if (TERMINAL.has(row.state)) {
      row.pausedReason = null
      if (row.state === 'SUCCEEDED') row.progressPct = 100
    }
  }
  if (str(d.target_world_id)) row.targetWorldId = String(d.target_world_id)
  if (Number.isFinite(tWallMs)) row.updatedMs = tWallMs
  return row
}

// ------------------------------------------------------------------ event batching (<= 4 Hz)
export const JOBS_FLUSH_MS = 250
const pending = new Map<string, { type: string; data: Record<string, unknown>; tWallMs: number }[]>()
let flushTimer: ReturnType<typeof setTimeout> | null = null
let lastFlushMs = Number.NEGATIVE_INFINITY
const nowMs = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function sortRows(rows: JobRow[]): JobRow[] {
  return rows.sort((a, b) => (Number.isFinite(b.createdMs) ? b.createdMs : 0) - (Number.isFinite(a.createdMs) ? a.createdMs : 0) || b.jobId.localeCompare(a.jobId))
}

/** apply the pending events in one store write */
export function flushJobs(): void {
  flushTimer = null
  if (!pending.size) return
  lastFlushMs = nowMs()
  const s = jobsStore.getState()
  const byId = new Map(s.jobs.map((r) => [r.jobId, r]))
  for (const [id, evs] of pending) {
    let row = byId.get(id)
    for (const e of evs) row = applyJobEvent(row, e.type, e.data, e.tWallMs)
    if (row) {
      if (!Number.isFinite(row.createdMs)) row = { ...row, createdMs: evs[0].tWallMs }
      byId.set(id, row)
    }
  }
  pending.clear()
  jobsStore.setState({ jobs: sortRows([...byId.values()]), version: s.version + 1, flushes: s.flushes + 1, status: s.status === 'idle' ? 'ready' : s.status })
}

function schedule(): void {
  if (flushTimer !== null) return
  const wait = Math.max(0, JOBS_FLUSH_MS - (nowMs() - lastFlushMs))
  flushTimer = setTimeout(flushJobs, wait)
}

/** feed job events (the RtClient event batch or tests); only job.state and job.progress are kept */
export function ingestJobEvents(batch: readonly Pick<RtEvent, 'type' | 'data' | 't_wall_ns'>[]): void {
  let any = false
  for (const ev of batch) {
    if (ev.type !== 'job.state' && ev.type !== 'job.progress') continue
    const d = (ev.data ?? {}) as Record<string, unknown>
    const id = typeof d.job_id === 'string' ? d.job_id : null
    if (!id) continue
    const w = Number(ev.t_wall_ns)
    const tWallMs = ev.t_wall_ns !== undefined && Number.isFinite(w) && w > 0 ? w / 1e6 : Date.now()
    const list = pending.get(id)
    const item = { type: ev.type, data: d, tWallMs }
    // progress keeps only the latest per job (M01 §7.2); state changes are all kept in order
    if (list) {
      if (ev.type === 'job.progress') {
        const i = list.findIndex((x) => x.type === 'job.progress')
        if (i >= 0) list.splice(i, 1)
      }
      list.push(item)
    } else pending.set(id, [item])
    any = true
  }
  if (any) schedule()
}

// ------------------------------------------------------------------ REST
function failStatus(e: unknown): { status: JobsStatus; code: number | null } {
  if (e instanceof ApiError) {
    if (e.status === 404 || e.status === 405) return { status: 'unavailable', code: 404 }
    if (e.status === 503 || e.reason === 213) return { status: 'unavailable', code: 213 }
    return { status: 'error', code: e.reason ?? e.status }
  }
  return { status: 'error', code: null }
}

export interface ReconSubmit {
  sourceWorldId: string
  path: 'helix' | 'lawnmower'
  frames: number
  targetWorldId?: string
  seed?: number
  georef?: 'gnss' | 'none'
}
export interface ReconEngineInfo { engine: string; variant: string | null; available: boolean; reason: string | null; engineScale: string | null; targetVersion: string | null }

let bound: RtClient | null = null
let unbind: (() => void) | null = null

export const jobs = {
  /** R39: list (newest first); keeps rows that events created meanwhile */
  async refresh(): Promise<void> {
    if (jobsStore.getState().status === 'idle') jobsStore.setState({ status: 'loading' })
    try {
      const r = await apiGet<{ items?: Record<string, unknown>[] }>('/api/jobs?limit=100')
      const rows = (r.items ?? []).map(jobFromWire)
      const byId = new Map(rows.map((x) => [x.jobId, x]))
      for (const old of jobsStore.getState().jobs) {
        const fresh = byId.get(old.jobId)
        // a REST snapshot older than the event state keeps the event progress
        if (fresh && old.attempt === fresh.attempt && old.progressPct > fresh.progressPct && !TERMINAL.has(fresh.state)) byId.set(old.jobId, { ...fresh, progressPct: old.progressPct })
        if (!fresh) byId.set(old.jobId, old)
      }
      const s = jobsStore.getState()
      jobsStore.setState({ jobs: sortRows([...byId.values()]), version: s.version + 1, status: 'ready', code: null })
    } catch (e) {
      jobsStore.setState(failStatus(e))
    }
  },
  /** R38: submit a Mock reconstruction; resolves the job id or throws ApiError (124, 330-346, 213) */
  async submitRecon(p: ReconSubmit): Promise<string> {
    const body: Record<string, unknown> = {
      engine: 'mock', source: { kind: 'world_sample', world_id: p.sourceWorldId, path: p.path, frames: Math.round(p.frames) },
      ...(p.targetWorldId ? { target_world_id: p.targetWorldId } : {}),
      ...(p.seed !== undefined ? { seed: p.seed } : {}),
      ...(p.georef ? { params: { georef: { mode: p.georef } } } : {}),
    }
    const key = `recon-${Date.now().toString(36)}-${Math.floor(performance.now() * 1000).toString(36)}`
    const r = await apiPost<{ job_id: string; state?: string; target_world_id?: string; session_id?: string }>('/api/recon/jobs', body, { headers: { 'Idempotency-Key': key } })
    const row = jobFromWire({ job_id: r.job_id, kind: 'recon', state: r.state ?? 'QUEUED', target_world_id: r.target_world_id ?? p.targetWorldId,
      created_unix_ns: Date.now() * 1e6, recon: { engine: 'mock', source_world_id: p.sourceWorldId, session_id: r.session_id, frames: { total: p.frames } } })
    const s = jobsStore.getState()
    if (!s.jobs.some((x) => x.jobId === row.jobId)) jobsStore.setState({ jobs: sortRows([row, ...s.jobs]), version: s.version + 1, status: 'ready' })
    return r.job_id
  },
  /** R41 */
  async cancel(id: string): Promise<void> {
    await apiPost(`/api/jobs/${encodeURIComponent(id)}/cancel`, {})
  },
  /** R42 (FAILED and resumable only) */
  async retry(id: string): Promise<void> {
    const key = `retry-${id}-${Date.now().toString(36)}`
    await apiPost(`/api/jobs/${encodeURIComponent(id)}/retry`, {}, { headers: { 'Idempotency-Key': key } })
  },
  /** R64: the last lines of the job log */
  async log(id: string, tail = 200): Promise<string[]> {
    const r = await apiGet<{ lines?: unknown[] }>(`/api/jobs/${encodeURIComponent(id)}/log?tail=${tail}`)
    return (r.lines ?? []).map((l) => (typeof l === 'string' ? l : JSON.stringify(l)))
  },
  /** R63 */
  async engines(): Promise<ReconEngineInfo[]> {
    const r = await apiGet<{ items?: Record<string, unknown>[] }>('/api/recon/engines')
    return (r.items ?? []).map((x) => ({
      engine: str(x.engine) ?? '', variant: str(x.variant), available: x.available === true, reason: str(x.reason),
      engineScale: str(x.engine_scale), targetVersion: str(x.target_version),
    }))
  },
}

/** subscribe to the job events of the page client (RtContext); the REST list is loaded by the Jobs page */
export function bindJobs(rt: RtClient | null): () => void {
  if (rt === bound) return () => {}
  unbind?.()
  bound = rt
  unbind = rt ? rt.onEvents((batch) => ingestJobEvents(batch)) : null
  return () => {
    if (bound === rt) {
      unbind?.()
      unbind = null
      bound = null
    }
  }
}

/** tests: forget rows, pending events and the binding */
export function resetJobs(): void {
  if (flushTimer !== null) clearTimeout(flushTimer)
  flushTimer = null
  pending.clear()
  lastFlushMs = Number.NEGATIVE_INFINITY
  unbind?.()
  unbind = null
  bound = null
  jobsStore.setState({ jobs: [], version: 0, status: 'idle', code: null, flushes: 0 })
}

// test builds (VITE_AWR_TEST_SWITCHES=1): perf/m15/fxweb2.spec.ts feeds job events and reads the store writes
if (TEST_SWITCHES && typeof window !== 'undefined') {
  ;(window as unknown as { __jobs?: unknown }).__jobs = { store: jobsStore, ingest: ingestJobEvents, refresh: () => jobs.refresh() }
}
