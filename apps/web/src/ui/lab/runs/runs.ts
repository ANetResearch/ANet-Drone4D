// /lab/runs 的数据形状与纯函数（AWR-17 §18.6、BN-42 至 BN-45；M27 §6.3；AWR-14 §21.6、§21.15.4、§21.15.6；D3-AC-51）。
// 列表项形状以 python/awr/bench/api/runs.py 为准：受管运行（managed = true）带状态机字段、排队位置、原因码与按请求者收窄的
// `actions{cancel, retry}`；目录运行（managed = false，采集工具与原型基准直接写入）状态取运行清单，不可取消或重试。
// 页面只按服务端给出的 `actions` 呈现操作，不在前端推断权限（服务端按 x-d3-role 与项目范围独立执行，AWR-05 ADR-166）。
import type { BenchRunItem } from '@/net/bench'
import type { ReasonView } from '@/ui/lab/worlds/worlds'

/** M27 §6.3 状态机 */
export const RUN_STATES = ['QUEUED', 'PREPARING', 'RUNNING', 'FINALIZING', 'EVALUATING', 'COMPLETED', 'FAILED', 'CANCELLED'] as const
export type RunState = (typeof RUN_STATES)[number]
/** 目录运行的清单状态（不经状态机） */
export const LEGACY_STATES = ['SUCCEEDED', 'EVALUATED', 'UNKNOWN'] as const
export const TERMINAL_STATES: ReadonlySet<string> = new Set(['COMPLETED', 'FAILED', 'CANCELLED'])
/** 取消请求的终止时限（s；M27-FR-024，与 runs_store.CANCEL_TIMEOUT_S 相同） */
export const CANCEL_TIMEOUT_S = 10
/** 列表轮询周期（ms；§21.6"列表 5 s 轮询，可见时"） */
export const RUNS_POLL_MS = 5_000
export const RUNS_PAGE = 100

export interface RunCancel { requested_unix_ns: number; by: string | null; deadline_unix_ns: number | null }
export interface RunActions { cancel: boolean; retry: boolean }
/** BN-42 列表项（受管与目录运行的并集） */
export interface RunItem extends BenchRunItem {
  managed: boolean; terminal: boolean; attempt: number | null; restart_count?: number; queue_position: number | null; blocked_by: string[]
  reason: ReasonView | null; cancel: RunCancel | null; actions: RunActions
  experiment_id?: string; tenant?: string; priority_class?: string; split?: string; ranked_valid?: boolean | null; submitted_by?: string
  queued_unix_ns?: number | null; started_unix_ns?: number | null; ended_unix_ns?: number | null; updated_unix_ns?: number | null
  seed_commit?: string | null; run_hash?: string | null
}
export interface RunsPage { items: RunItem[]; next_cursor: string | null; total: number }
export interface RunAttempt { attempt: number; state: string; restart_count: number; dir: string | null; started_unix_ns: number | null; ended_unix_ns: number | null; reason: ReasonView | null }
export interface RunTransition { attempt: number; from: string | null; to: string; reason_code: number | null; actor: string | null; t_unix_ns: number }
/** BN-43 */
export interface RunDetail extends RunItem { attempts?: RunAttempt[]; transitions?: RunTransition[] }

// ── 筛选与 URL ───────────────────────────────────────────────────────────────────────────────────────────────────────

/** URL 查询参数（§21.2.2：event、xp、policy、state、ranked、cursor） */
export interface RunFilter { event: string | null; xp: string | null; policy: string | null; state: string[]; ranked: boolean | null; cursor: string | null }
const KNOWN_STATES: ReadonlySet<string> = new Set([...RUN_STATES, ...LEGACY_STATES])

export function parseRunFilter(s: URLSearchParams): RunFilter {
  const r = s.get('ranked')
  return {
    event: s.get('event') || null, xp: s.get('xp') || null, policy: s.get('policy') || null,
    state: (s.get('state') ?? '').split(',').map((x) => x.trim()).filter((x) => KNOWN_STATES.has(x)),
    ranked: r === 'true' || r === '1' ? true : r === 'false' || r === '0' ? false : null,
    cursor: s.get('cursor') || null,
  }
}

/** BN-42 查询串：xp 映射为接口参数 experiment；未知状态值在解析时已丢弃（服务端对其返回 400 300） */
export function runsQuery(f: RunFilter, limit = RUNS_PAGE): string {
  const q = new URLSearchParams()
  if (f.event) q.set('event', f.event)
  if (f.xp) q.set('experiment', f.xp)
  if (f.policy) q.set('policy', f.policy)
  if (f.state.length) q.set('state', f.state.join(','))
  if (f.ranked !== null) q.set('ranked', String(f.ranked))
  if (f.cursor) q.set('cursor', f.cursor)
  q.set('limit', String(limit))
  return `?${q.toString()}`
}

/** 页面 URL 的筛选补丁（updateSearch 的形状；筛选变化时回到第一页） */
export function filterPatch(f: Partial<RunFilter>): Record<string, string | null> {
  const out: Record<string, string | null> = {}
  if ('event' in f) out.event = f.event || null
  if ('xp' in f) out.xp = f.xp || null
  if ('policy' in f) out.policy = f.policy || null
  if ('state' in f) out.state = f.state?.length ? f.state.join(',') : null
  if ('ranked' in f) out.ranked = f.ranked === null || f.ranked === undefined ? null : String(f.ranked)
  out.cursor = 'cursor' in f ? f.cursor || null : null
  return out
}

export const hasFilter = (f: RunFilter): boolean => Boolean(f.event || f.xp || f.policy || f.state.length || f.ranked !== null)

// ── 状态与操作 ─────────────────────────────────────────────────────────────────────────────────────────────────────

export const isActive = (r: Pick<RunItem, 'managed' | 'state'>): boolean => r.managed && !TERMINAL_STATES.has(r.state)

/** 进度列的形态：排队（位置与阻塞原因）、取消中（时限）、运行中、终态 */
export type RunPhase =
  | { kind: 'queued'; position: number | null; blocked: string[] }
  | { kind: 'cancelling'; remainS: number | null; overdue: boolean }
  | { kind: 'active' }
  | { kind: 'done' }
export function runPhase(r: RunItem, nowNs: number): RunPhase {
  if (!r.managed || TERMINAL_STATES.has(r.state)) return { kind: 'done' }
  if (r.cancel && r.cancel.deadline_unix_ns !== null) {
    const remain = (r.cancel.deadline_unix_ns - nowNs) / 1e9
    return { kind: 'cancelling', remainS: Math.max(0, Math.ceil(remain)), overdue: remain < 0 }
  }
  if (r.state === 'QUEUED') return { kind: 'queued', position: r.queue_position, blocked: r.blocked_by }
  return { kind: 'active' }
}

/** 状态副文：PREPARING 且 restart_count > 0 时为"第 k 次重启"（§21.6） */
export const restartOf = (r: Pick<RunItem, 'state' | 'restart_count'>): number => (r.state === 'PREPARING' && (r.restart_count ?? 0) > 0 ? (r.restart_count as number) : 0)

/**
 * 表格唯一的 hot 行（§21.6：最新一条 FAILED 行，RedArbiter 的表格候选）：按结束时刻、更新时刻取最新；同刻按 run_id 决胜
 */
export function hotFailed(rows: readonly RunItem[]): string | null {
  let best: RunItem | null = null
  const t = (r: RunItem) => r.ended_unix_ns ?? r.updated_unix_ns ?? r.created_unix_ns ?? 0
  for (const r of rows) {
    if (r.state !== 'FAILED') continue
    if (!best || t(r) > t(best) || (t(r) === t(best) && r.run_id > best.run_id)) best = r
  }
  return best?.run_id ?? null
}

/** 可批量取消的运行（服务端给出 actions.cancel = true） */
export const cancellable = (rows: readonly RunItem[]): RunItem[] => rows.filter((r) => r.actions.cancel)

/** 选择集与当前页可取消运行求交（轮询后已终态或已登记取消的行自动移出选择） */
export function pruneSelection(sel: ReadonlySet<string>, rows: readonly RunItem[]): Set<string> {
  const ok = new Set(cancellable(rows).map((r) => r.run_id))
  return new Set([...sel].filter((id) => ok.has(id)))
}

/** 有活动运行时按 5 s 轮询，否则 30 s（页面不可见时 TanStack Query 不轮询） */
export const pollInterval = (rows: readonly RunItem[] | undefined): number => (rows?.some(isActive) ? RUNS_POLL_MS : 30_000)

/** 待确认的操作：单个运行的取消或重试，或批量取消 */
export type Confirm = { kind: 'cancel' | 'retry'; run: RunItem } | { kind: 'batch'; ids: string[] }

// ── 原因码 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

/** 原因码的界面短文案（§21.15.6）：先取 lab 词典 `lab.reason.<code>`，否则用服务端中文文案（中文界面）或原因名 */
export interface ReasonLine { code: number; name: string; short: string; remedy: string }
export function reasonLine(r: ReasonView, zh: boolean, dict: { has: (k: string) => boolean; t: (k: string) => string }): ReasonLine {
  const k = `lab.reason.${r.code}`
  const name = r.name ?? ''
  const short = dict.has(k) ? dict.t(k) : zh && r.message ? r.message : name
  return { code: r.code, name, short, remedy: zh ? r.remedy ?? '' : '' }
}

/** 拒绝原因的 `why`（problem+json detail.why） */
export function whyOf(detail: unknown): string {
  return detail && typeof detail === 'object' && 'why' in detail ? String((detail as { why: unknown }).why) : ''
}
