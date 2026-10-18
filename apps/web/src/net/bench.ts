// bench-api client (AWR-17 §18.3, §18.4; D3 internal workspace, M15 lab pages). Auth is the workspace session cookie
// `d3_session` (HttpOnly, SameSite=Strict), so requests go out with credentials 'same-origin' and never carry a token in
// the URL; writes add `X-D3-CSRF: 1` and a JSON body (§18.3.1 item 3). Errors are problem+json: BenchError carries the
// HTTP status, the reason code and its name (600-699 codes are not in the D2 web dictionary, the page shows code + name).
export const BENCH = '/api/bench/v1'

export class BenchError extends Error {
  constructor(readonly status: number, readonly code: number | null, readonly reasonName: string | null, message: string,
    readonly detail: unknown = null) {
    super(message)
  }
}

interface Problem { code?: number; name?: string; message?: string; detail?: unknown }

async function toError(r: Response, path: string): Promise<BenchError> {
  let p: Problem = {}
  try {
    p = (await r.json()) as Problem
  } catch {
    p = {}
  }
  return new BenchError(r.status, typeof p.code === 'number' ? p.code : null, p.name ?? null, p.message ?? `${r.status} ${path}`, p.detail ?? null)
}

async function req(path: string, init: RequestInit = {}): Promise<Response> {
  const write = init.method !== undefined && init.method !== 'GET'
  const headers: Record<string, string> = { ...(init.headers as Record<string, string> | undefined) }
  if (write) {
    headers['X-D3-CSRF'] = '1'
    if (init.body !== undefined) headers['Content-Type'] = 'application/json'
  }
  const r = await fetch(BENCH + path, { ...init, headers, credentials: 'same-origin' })
  if (!r.ok && r.status !== 304) throw await toError(r, path)
  return r
}

export async function benchGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await (await req(path, { signal })).json()) as T
}

export interface BenchBytes { buf: ArrayBuffer; headers: Headers }
export async function benchBytes(path: string, signal?: AbortSignal): Promise<BenchBytes> {
  const r = await req(path, { signal })
  return { buf: await r.arrayBuffer(), headers: r.headers }
}

export async function benchPost<T>(path: string, body: unknown): Promise<T | null> {
  const r = await req(path, { method: 'POST', body: JSON.stringify(body) })
  return r.status === 204 ? null : ((await r.json()) as T)
}

export async function benchDelete(path: string): Promise<void> {
  await req(path, { method: 'DELETE' })
}

export interface BenchSession { principal: string; role: 'invited' | 'researcher' | 'evaluator' | 'admin'; projects: string[]; via: string }
export type BoardMetricKey = 'ec' | 'eu' | 'ao' | 'ce'
export type Components = Record<BoardMetricKey, number | null>
export interface BenchRunEvaluation { components: Components; ranked_valid: boolean; invalid_reasons: string[] }
export interface BenchRunItem {
  run_id: string; event_id: string; state: string; has_frames: boolean; created_unix_ns: number | null; kind: string | null; world_id: string | null
  /** Env benchmark runs only (kind = bench) */
  policy?: string | null; physics?: string | null; obs_level?: string | null; ranked?: boolean; n_uav?: number; duration_s?: number | null
  evaluation?: BenchRunEvaluation | null
}
/** bench/board.v1 (BN-97) */
export interface BoardMetric { value: number | null; ci95: [number, number]; rank?: number; tie_group?: number }
export interface BoardEntry {
  entry_id: string; team: string; display_name: string; track: string; lane: string; metrics: Record<BoardMetricKey, BoardMetric>
  dominance_tier: number | null; status?: 'valid' | 'invalid'; first_listed_unix_ns: number; baseline: boolean
}
export interface BoardTrack { track: string; fleet_ref: string; obs_level: string; physics: string; mode: string; agent_class: string; appearance: string }
export interface BoardLane { lane: string; kind: string; event_type?: string; test_events: string[]; seed_set_commit: string }
export interface Board {
  schema: string; board_version: string; tracks: BoardTrack[]; lanes: BoardLane[]; seed_set_commit: string; evaluator_major: number
  platform_digest: string; entries: BoardEntry[]
}
/** BN-83 */
export interface BoardVersion { board_version: string; current: boolean; score_version: string; weights: null; tracks: BoardTrack[]; lanes: BoardLane[]; n_entries: number }
export interface BoardIndex { current: string | null; versions: BoardVersion[] }
/** bench/submission.v1 (BN-85, BN-95, BN-96) */
export type SubmissionState = 'RECEIVED' | 'SCANNING' | 'VALIDATING' | 'EVALUATING' | 'SCORING' | 'DONE' | 'REJECTED' | 'FAILED' | 'WITHDRAWN'
export interface SubmissionLane {
  lane: string; metrics: Partial<Record<BoardMetricKey, BoardMetric>>; dominance_tier?: number
  runs: { total: number; valid: number; invalid: number; platform_rerun?: number }; status?: string; composite?: number | null
}
export interface Submission {
  schema: string; submission_id: string; track: string; lanes?: string[]; image_digest: string; entry: { cmd: string[]; env?: Record<string, string> }
  notes?: string; state: SubmissionState; visibility: 'private' | 'board'; position: number
  reason?: { code: number; name: string; detail?: Record<string, unknown> }; result?: { lanes: SubmissionLane[] }
}
/** BN-84 */
export interface BoardEntryEvent { event: string; n_runs: number; invalid_runs: number; invalid_reasons: string[]; seeds: number[]; metrics: Components; repeat_delta: null }
export interface BoardEntryDetail {
  board_version: string; entry_id: string; track: string; display_name: string; team: string; baseline: boolean
  lanes: { lane: string; metrics: Record<BoardMetricKey, BoardMetric>; dominance_tier: number; events: BoardEntryEvent[] }[]
}
export interface BenchRunChannel { uav_id: string; sensor_id: string; latest_seq: number | null }
export interface BenchRunEvent { event_id: string; version: number; offset_s: number }
export interface BenchRunFleet { uav: string; path: { kind: 'orbit'; center?: [number, number]; radius_m: number; period_s: number; phase_deg?: number; ccw?: boolean } | { kind: 'waypoints'; points: [number, number, number, number, number][] } }
export interface BenchRun {
  run_id: string; event_id: string; state: string; kind: string | null; world_id: string | null; frames?: BenchRunChannel[]
  event?: BenchRunEvent; fleet?: BenchRunFleet[]
}
export interface FrameIndexItem { seq: number; t_ns: number; bytes: number; sha256: string }
export interface FrameIndexPage { run_id: string; uav_id: string; sensor_id: string; period_ns: number; items: FrameIndexItem[]; next_seq: number | null }

/** all index items of one channel (follows next_seq; 1000 items per page) */
export async function frameIndex(rid: string, uav: string, sensor: string, signal?: AbortSignal): Promise<{ periodNs: number; items: FrameIndexItem[] }> {
  const items: FrameIndexItem[] = []
  let next: number | null = 0
  let periodNs = 100_000_000
  while (next !== null) {
    const page: FrameIndexPage = await benchGet<FrameIndexPage>(`/runs/${encodeURIComponent(rid)}/frames/${encodeURIComponent(uav)}/${encodeURIComponent(sensor)}?seq_from=${next}`, signal)
    items.push(...page.items)
    periodNs = page.period_ns
    next = page.next_seq
  }
  return { periodNs, items }
}

export const ROLE_RANK: Readonly<Record<BenchSession['role'], number>> = { invited: 0, researcher: 1, evaluator: 2, admin: 3 }
