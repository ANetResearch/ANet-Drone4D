// /lab/worlds、/lab/worlds/:wid、/lab/events 的数据形状与纯函数（AWR-17 §18.5.2、§18.5.3、§18.7；M22 §7.6.4；M23 §7.3.1；
// AWR-14 §21.3、§21.4.1；D3-AC-51）。接口形状以 python/awr/bench/api/worlds.py 为准：世界列表项与信封为 snake_case，世界摘要沿用
// 世界包的 camelCase。本文件不依赖 net/**，页面组件负责取数。
//
// 标定显示规则（§21.3.2）：标定数字带单位与条件（"8.374 m/单位（OSM 道路配准，残差 1.9 m）"）；调平两法差值 > 0.5° 时为 warning
// （红描边 + TriangleAlert）并提示控制点复核；`scaleStatus = assumed` 时说明尺度为人工设定。

/** BN-10 列表项 */
export interface WorldItem {
  world_id: string; name: string | null; name_zh: string | null; role: string | null; profile: string | null
  content_version: string | null; scale_status: string | null; metric_ok: boolean; ranked_ok: boolean; metric_reason: string | null
  bounds_m: { xmin: number | null; xmax: number | null; ymin: number | null; ymax: number | null }
  layers_count: number; license_id: string | null; qa_status: string | null; events_count: number
}

export interface WorldLeveling { primary: string | null; demDeg: number | null; wallDeg: number | null; deltaDeg: number | null; planesMaxSlopeDeg: number | null }
export interface WorldQuality { coverageFrac?: number | null; nodataFrac?: number | null; holeFrac?: number | null; stretchedFrac?: number | null; multilayerFrac?: number | null }
export interface WorldSensorHints {
  texelM?: number | null; meshEdgeP50M?: number | null; textureLimitedRangeM?: Record<string, number> | null
  bakedSunAzElDeg?: [number, number] | null; bakedSunConfidence?: string | null
}
export interface WorldDerived { name?: string; version?: string; license?: string; use?: string; attribution?: string; sha256?: string; url?: string }
/** M22 §7.6.4 世界摘要（BN-11 `summary`） */
export interface WorldSummary {
  id: string; name: string | null; nameZh: string | null; role: string | null; profile?: string | null; contentVersion: string | null
  scaleStatus: string | null
  metric: { ok: boolean; rankedOk: boolean; reason?: string | null; missing?: string[] }
  registration: { method: string | null; unitsToMeters: number | null; yawDeg: number | null; leveledDeg: number | null; rmseM: number | null; leveling: WorldLeveling } | null
  trueNorth: { yawOffsetDeg?: number | null; confidence?: string | null; evidence?: string[] } | null
  anchor: { latDeg: number | null; lonDeg: number | null; hMslM: number | null; uncertaintyM: number | { horizontal?: number; vertical?: number } | null } | null
  bounds: { min: [number, number, number]; max: [number, number, number] } | null
  sensorHints: WorldSensorHints | null
  quality: WorldQuality | null
  layers: { id: string; type: string; format: string; bytes: number | null }[]
  anchorsCount: { auto: number; review: number; confirmed: number }
  license: { dataset: string | null; citation: string | null; notice: string | null; derivedFrom: WorldDerived[] }
  qa: { status: string | null; messages: unknown[] }
  build?: { createdAt: string | null; generatorVersion: string | null; profile: string | null }
}

/** 原因码载荷（AWR-17 §18.5.3 `reason` 加中文文案；bench-api `reason_view`） */
export interface ReasonView { code: number; name: string | null; message: string | null; remedy: string | null; detail: unknown }

export type EventStatus = 'draft' | 'queued' | 'compiling' | 'compiled' | 'infeasible' | 'failed' | 'published' | 'deprecated'
export const EVENT_STATUSES: readonly EventStatus[] = ['draft', 'queued', 'compiling', 'compiled', 'infeasible', 'failed', 'published', 'deprecated']

/** BN-20 列表项（每个事件的最新版本） */
export interface EventItem {
  event_id: string; version: number; split?: string; status: EventStatus; strict: boolean; name: string | null; event_type: string | null
  world: { id: string | null; content_version: string | null }; duration_s: number | null; n_scripted: number; n_background: number
  tape_content_sha256: string | null; reason: ReasonView | null; updated_unix_ns: number; versions: number
}
export interface GateIssue { code?: string; reason?: number; path?: string; message_zh?: string; message_en?: string; message?: string }
export interface EventGate { gate: number; /** pass、warn、fail、skipped */ status: string; errors: GateIssue[]; warnings: GateIssue[]; duration_ms?: number }
/** BN-22 编译状态载荷（M23 §7.3.1） */
export interface EventDetail extends Omit<EventItem, 'versions'> {
  gates: EventGate[]; segments: Record<string, unknown>[]; retime: Record<string, unknown>
  tape: { content_sha256?: string | null; body_sha256?: string | null; n_actors?: number | null; n_ticks?: number | null; bytes?: number | null } | null
  truth_summary: unknown; provenance: unknown
  published: { by: string | null; published_unix_ns: number | null; content_sha256: string | null } | null
  artifacts: { name: string; bytes: number }[]
  latest_version: number; versions: { version: number; status: EventStatus }[]
}

/** 世界包、事件与运行 id（bench-api 路径参数同一规则；不合规时页面回到对应列表页，AWR-14 §21.2.2 规则 1） */
export const LAB_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

// ── 世界 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

export const WORLD_TABS = ['overview', 'calibration', 'quality', 'layers', 'sensors', 'license'] as const
export type WorldTab = (typeof WORLD_TABS)[number]
export function parseWorldTab(v: string | null): WorldTab {
  return (WORLD_TABS as readonly string[]).includes(v ?? '') ? (v as WorldTab) : 'overview'
}

/** 调平两法差值告警门限（°；M22 V-GLB-15，AWR-14 §21.3.2） */
export const LEVELING_DELTA_WARN_DEG = 0.5

/** 两法调平差值：优先取记录的 deltaDeg，缺省时由两法倾角求差；两者都缺时为 null */
export function levelingDelta(l: WorldLeveling | null | undefined): number | null {
  if (!l) return null
  if (typeof l.deltaDeg === 'number') return Math.abs(l.deltaDeg)
  return typeof l.demDeg === 'number' && typeof l.wallDeg === 'number' ? Math.abs(l.demDeg - l.wallDeg) : null
}
export const levelingWarn = (l: WorldLeveling | null | undefined): boolean => (levelingDelta(l) ?? 0) > LEVELING_DELTA_WARN_DEG

/** 米制状态的 i18n 后缀：可排名、仅米制、不可用 */
export function metricKey(ok: boolean, rankedOk: boolean): 'ranked' | 'metric' | 'none' {
  return ok && rankedOk ? 'ranked' : ok ? 'metric' : 'none'
}

/** 范围（m）：x 向 × y 向 */
export function extentM(b: WorldItem['bounds_m'] | null | undefined): [number, number] | null {
  if (!b || [b.xmin, b.xmax, b.ymin, b.ymax].some((v) => typeof v !== 'number')) return null
  return [(b.xmax as number) - (b.xmin as number), (b.ymax as number) - (b.ymin as number)]
}
export function summaryExtent(s: WorldSummary): { x: number; y: number; zmin: number; zmax: number } | null {
  if (!s.bounds) return null
  return { x: s.bounds.max[0] - s.bounds.min[0], y: s.bounds.max[1] - s.bounds.min[1], zmin: s.bounds.min[2], zmax: s.bounds.max[2] }
}

/** 世界显示名：中文界面优先 nameZh */
export function worldName(w: { name: string | null; nameZh?: string | null; name_zh?: string | null }, zh: boolean): string {
  const z = w.nameZh ?? w.name_zh ?? null
  return (zh ? z ?? w.name : w.name ?? z) ?? ''
}

/** 锚点不确定度：数值或 {horizontal, vertical}（m） */
export function anchorUncertainty(u: NonNullable<WorldSummary['anchor']>['uncertaintyM']): { h: number | null; v: number | null } {
  if (typeof u === 'number') return { h: u, v: u }
  return { h: u?.horizontal ?? null, v: u?.vertical ?? null }
}

/** QA 消息：字符串或 {level, code, message} */
export function qaMessage(m: unknown): string {
  if (typeof m === 'string') return m
  if (m && typeof m === 'object') {
    const o = m as Record<string, unknown>
    return [o.level, o.code, o.message ?? o.msg].filter((x) => typeof x === 'string' && x).join(' · ')
  }
  return ''
}

// ── 事件 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

export interface EventFilter { world: string | null; status: EventStatus | null; q: string }
export function parseEventFilter(s: URLSearchParams): EventFilter {
  const st = s.get('status')
  return { world: s.get('world') || null, status: (EVENT_STATUSES as readonly string[]).includes(st ?? '') ? (st as EventStatus) : null, q: (s.get('q') ?? '').slice(0, 80) }
}
/** BN-20 查询串（world、status、q；缺省项不写） */
export function eventQuery(f: EventFilter): string {
  const q = new URLSearchParams()
  if (f.world) q.set('world', f.world)
  if (f.status) q.set('status', f.status)
  if (f.q.trim()) q.set('q', f.q.trim())
  const s = q.toString()
  return s ? `?${s}` : ''
}

/** 哈希显示：前 12 位（AWR-14 §21.15.3 规则 2） */
export const shortHash = (h: string | null | undefined): string => (h ? h.slice(0, 12) : '—')

/** 门问题的本地化文案：按界面语言取 message_zh 或 message_en，缺省回退另一种语言与 code */
export function issueText(i: GateIssue, zh: boolean): string {
  const m = zh ? i.message_zh ?? i.message_en : i.message_en ?? i.message_zh
  return [i.code, m ?? i.message].filter(Boolean).join(' · ')
}

/** 失败门（status = fail）在前，其余按门序 */
export function gateOrder(gates: readonly EventGate[]): EventGate[] {
  return gates.toSorted((a, b) => Number(b.status === 'fail') - Number(a.status === 'fail') || a.gate - b.gate)
}

// ── 通用格式 ───────────────────────────────────────────────────────────────────────────────────────────────────────

/** UTC 纳秒 → `YYYY-MM-DD HH:MM:SS UTC`（墙钟一律以 UTC 显示，避免多时区成员误读） */
export function fmtUnixNs(ns: number | null | undefined): string {
  if (typeof ns !== 'number' || !Number.isFinite(ns) || ns <= 0) return '—'
  return `${new Date(Math.floor(ns / 1e6)).toISOString().slice(0, 19).replace('T', ' ')} UTC`
}
/** ISO 时刻字符串 → 同上格式 */
export function fmtIso(s: string | null | undefined): string {
  if (!s) return '—'
  const t = Date.parse(s)
  return Number.isFinite(t) ? fmtUnixNs(t * 1e6) : s
}
/** 数值：固定小数位，缺省为 "—"；负号用 U+2212（AWR-14 §13.3） */
export function fmtFixed(v: number | null | undefined, digits: number): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  const s = v.toFixed(digits)
  return s.startsWith('-') ? `−${s.slice(1)}` : s
}
/** 占比（0..1）→ 百分数文本 */
export const fmtFrac = (v: number | null | undefined, digits = 1): string => (typeof v === 'number' && Number.isFinite(v) ? `${fmtFixed(v * 100, digits)}%` : '—')
