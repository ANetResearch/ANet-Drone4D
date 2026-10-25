// 运行回放 · 数据检视的纯函数（AWR-14 §21.2.2、§21.9；AWR-17 §18.10.2 BN-57；AWR-05 §6.1.4 至 §6.1.6；D3-AC-25）：查询参数、
// viewer_url 请求与响应校验（链接不含凭据、694 版本检查）、公开静态切片的来源校验（只读静态制品，不发起 catalog 请求）、设备档判定、
// 错误归类与原生 Viewer 命令。不引用 net/bench.ts 与应用路由：面板同时用于 /lab/ 公开只读构建（F07 样例页），该构建剔除这些模块。
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { RERUN_VIEWER_VERSION } from './viewerLoader'

/** 运行 id（与 /lab/runs/:rid 路由的校验相同） */
export const INSPECT_RUN_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
export type CatalogKind = 'dist' | 'eval'
/** OPENING 超时（ms，§21.9.2：10 s 无 recording_open → ERROR） */
export const OPEN_TIMEOUT_MS = 10_000
/** 公开切片单片上限（AWR-05 §6.1.5：≤ 64 MB，MB 为 2^20 B，与 scan_public 相同） */
export const SLICE_MAX_BYTES = 64 * 2 ** 20
/** 底图上限（≤ 24 MB） */
export const BASEMAP_MAX_BYTES = 24 * 2 ** 20

export interface InspectQuery {
  /** 打开后 seek 到的仿真秒 */
  t: number | null
  /** 时间窗（仿真秒，`slice=<from>,<to>`），传给 viewer_url 的 t_from、t_to */
  slice: [number, number] | null
  catalog: CatalogKind
}

const num = (s: string | null): number | null => {
  if (s === null || s.trim() === '') return null
  const v = Number(s)
  return Number.isFinite(v) && v >= 0 ? v : null
}

export function parseInspectQuery(q: URLSearchParams): InspectQuery {
  const parts = (q.get('slice') ?? '').split(',')
  const a = num(parts[0] ?? null)
  const b = num(parts[1] ?? null)
  return { t: num(q.get('t')), slice: a !== null && b !== null && b > a ? [a, b] : null, catalog: q.get('catalog') === 'eval' ? 'eval' : 'dist' }
}

/** BN-57 请求路径（相对 bench-api 前缀） */
export function viewerUrlPath(rid: string, q: Pick<InspectQuery, 'slice' | 'catalog'>): string {
  const p = new URLSearchParams()
  if (q.slice) {
    p.set('t_from', String(q.slice[0]))
    p.set('t_to', String(q.slice[1]))
  }
  p.set('catalog', q.catalog)
  return `/runs/${encodeURIComponent(rid)}/viewer_url?${p.toString()}`
}

/** BN-57 响应（AWR-17 §18.10.2） */
export interface ViewerLink {
  url: string
  recording_id: string
  timeline: string
  time_range_ns: [number, number] | null
  catalog: CatalogKind
  rerun_version: string
  cookie_exp_unix_ns: string
  migrate_hint: string | null
}

/** "0.38.1" → "0.38"；不可解析为 null */
export function minorOf(v: string | null | undefined): string | null {
  const m = /^(\d+)\.(\d+)(?:\.|$)/.exec(v ?? '')
  return m ? `${m[1]}.${m[2]}` : null
}
/** 录制版本与锁定版本是否同一 minor（.rrd 只保证读同一 minor，跨 minor 须 rerun rrd migrate） */
export const sameMinor = (v: string | null | undefined): boolean => minorOf(v) !== null && minorOf(v) === minorOf(RERUN_VIEWER_VERSION)

// 凭据形态：查询参数名或令牌前缀（17 §3.2 禁止 URL 携带令牌；d3_rr 只在 Path=/rerun/ 的 HttpOnly Cookie 中）
const CREDENTIAL_RE = /[?&#](?:token|access_token|auth|authorization|key|sig|signature|d3_rr|d3_session)=|d3(?:st|rr|bt)_[A-Za-z0-9]|\/\/[^/@]+@/i
/** 链接是否含凭据（含则拒绝打开） */
export const hasCredential = (url: string): boolean => CREDENTIAL_RE.test(url)

export type InspectErrorKind = 'version' | 'role' | 'notFound' | 'session' | 'timeout' | 'load' | 'credential' | 'static' | 'unknown'
export interface InspectError { kind: InspectErrorKind; detail: string | null }

/** viewer_url 响应校验：结构、凭据与版本 */
export function checkLink(link: ViewerLink, expect?: { catalog?: CatalogKind }): InspectError | null {
  if (!link || typeof link.url !== 'string' || typeof link.recording_id !== 'string' || !link.recording_id) return { kind: 'unknown', detail: 'viewer_url' }
  if (hasCredential(link.url)) return { kind: 'credential', detail: null }
  if (!sameMinor(link.rerun_version) || link.migrate_hint) return { kind: 'version', detail: link.migrate_hint ?? link.rerun_version ?? null }
  if (expect?.catalog && link.catalog !== expect.catalog) return { kind: 'unknown', detail: `catalog ${String(link.catalog)}` }
  return null
}

/** HTTP 与原因码 → 错误类别（按 BenchError 的 status、code 字段鸭子类型判定，不引用 net/bench.ts） */
export function errorOf(e: unknown): InspectError {
  const x = e as { status?: unknown; code?: unknown; message?: unknown } | null
  const code = typeof x?.code === 'number' ? x.code : null
  const status = typeof x?.status === 'number' ? x.status : null
  const detail = typeof x?.message === 'string' ? x.message : null
  if (code === 694) return { kind: 'version', detail }
  if (code === 115 || status === 403) return { kind: 'role', detail }
  if (code === 305 || status === 404) return { kind: 'notFound', detail }
  if (status === 401 || code === 302) return { kind: 'session', detail }
  return { kind: 'unknown', detail }
}

// ── 公开站：静态切片（AWR-05 §6.1.5、§6.1.6；AWR-14 §21.9.4 第 3 行、§21.14.2） ─────────────────────────────────────────────

export interface StaticFile { url: string; bytes?: number | null }
export interface StaticSource {
  kind: 'static'
  /** 切片与底图共同的 recording_id（= run_id） */
  recordingId: string
  slices: readonly StaticFile[]
  basemap: StaticFile | null
  /** Rerun Viewer Wasm 的 ModelScope 副本（ADR-221：emax 出口约 3 Mbps）；缺省从 /lab/assets/ 自托管加载 */
  wasmUrl?: string | null
}
export interface CatalogSource {
  kind: 'catalog'
  catalog: CatalogKind
  /** 取 BN-57 viewer_url（设置 d3_rr Cookie）；公开构建不提供 */
  fetchLink: (signal: AbortSignal) => Promise<ViewerLink>
}
export type InspectSource = StaticSource | CatalogSource

/** ModelScope 上的公开静态制品（lab_static_index.v1 `external`，ADR-201、ADR-221） */
export const MODELSCOPE_URL_RE = /^https:\/\/(www\.)?modelscope\.cn\/[^\s"'<>\\]*$/

/** 公开静态制品的路径（相对路径，本站 /lab-static/ 与 /lab/ 前缀下的 .rrd，或 ModelScope 上的 .rrd）；拒绝 catalog、bench-api、
 * rerun-proxy 与其他外站地址 */
export function isStaticRrd(url: string): boolean {
  if (MODELSCOPE_URL_RE.test(url)) return /\.rrd(?:[?#]|$)/.test(url) && !url.includes('..')
  if (/^[a-z][a-z0-9+.-]*:/i.test(url) || url.startsWith('//')) return false        // 任何协议（http、rerun+http …）与协议相对地址
  if (/^\/(?!lab-static\/|lab\/)/.test(url)) return false                            // 站内只允许静态制品前缀
  if (/(^|\/)(api|rerun)(\/|$)/.test(url) || url.includes('..')) return false
  return /\.rrd(?:[?#]|$)/.test(url)
}

export function checkStaticSource(s: StaticSource): InspectError | null {
  const files = [...s.slices, ...(s.basemap ? [s.basemap] : [])]
  if (!INSPECT_RUN_RE.test(s.recordingId) || !s.slices.length) return { kind: 'static', detail: s.recordingId }
  for (const f of files) {
    if (!isStaticRrd(f.url)) return { kind: 'static', detail: f.url }
  }
  if (s.wasmUrl && !(MODELSCOPE_URL_RE.test(s.wasmUrl) && /\.wasm(?:[?#]|$)/.test(s.wasmUrl))) return { kind: 'static', detail: s.wasmUrl }
  for (const f of s.slices) if (typeof f.bytes === 'number' && f.bytes > SLICE_MAX_BYTES) return { kind: 'static', detail: f.url }
  if (s.basemap && typeof s.basemap.bytes === 'number' && s.basemap.bytes > BASEMAP_MAX_BYTES) return { kind: 'static', detail: s.basemap.url }
  return null
}

/** 静态来源一次打开的地址列表：切片在前、底图在后（同一 recording_id 合并加载） */
export const staticUrls = (s: StaticSource): string[] => [...s.slices.map((f) => f.url), ...(s.basemap ? [s.basemap.url] : [])]

// ── 设备档（§6.1.6 设备档行；§21.9.2 UNSUPPORTED） ───────────────────────────────────────────────────────────────────────

/** 软件光栅器（与 viewport/backend/deviceClass.ts SOFTWARE_RE 相同；测试核对两者一致） */
export const SOFTWARE_RENDERER_RE = /swiftshader|llvmpipe|softpipe|software|basic render/i
/** 数据检视不在手机与平板布局中提供（AWR-05 §7.9）：窄于该宽度或粗指针设备不启用 */
export const MIN_LAYOUT_PX = 1024

export interface DeviceProbe { webgl2: boolean; webgpu: boolean; renderer: string; simd: boolean; coarse: boolean; widthPx: number }
export type UnsupportedReason = 'software' | 'nogpu' | 'simd' | 'layout'
export type Support = { ok: true } | { ok: false; reason: UnsupportedReason }

/** 只在 Tier B/A（硬件 GPU）启用：Tier S（软件渲染）、WebGL2 与 WebGPU 均不可用、无 Wasm SIMD、手机与平板布局 → UNSUPPORTED */
export function decideSupport(p: DeviceProbe): Support {
  if (p.coarse || p.widthPx < MIN_LAYOUT_PX) return { ok: false, reason: 'layout' }
  if (!p.webgl2 && !p.webgpu) return { ok: false, reason: 'nogpu' }
  if (p.renderer && SOFTWARE_RENDERER_RE.test(p.renderer)) return { ok: false, reason: 'software' }
  if (!p.simd) return { ok: false, reason: 'simd' }
  return { ok: true }
}

/**
 * 页面使用的设备判定：开发与测试构建（TEST_SWITCHES）中 `?inspect=force` 跳过设备档判定，供 Playwright 在本机软件渲染下驱动真实
 * Viewer（R07 `lab/inspect`、`lab/public-samples`）；普通生产构建中该参数被常量折叠剔除。
 */
export function inspectSupport(search: URLSearchParams, probe: () => DeviceProbe = probeDevice): Support {
  if (TEST_SWITCHES && search.get('inspect') === 'force') return { ok: true }
  return decideSupport(probe())
}

const SIMD_PROBE = new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11])

/** 浏览器探测（临时画布读取 UNMASKED_RENDERER，随即释放上下文） */
export function probeDevice(): DeviceProbe {
  let webgl2 = false
  let renderer = ''
  try {
    const c = document.createElement('canvas')
    const gl = c.getContext('webgl2')
    if (gl) {
      webgl2 = true
      const ext = gl.getExtension('WEBGL_debug_renderer_info')
      renderer = String(gl.getParameter(ext ? ext.UNMASKED_RENDERER_WEBGL : gl.RENDERER) ?? '')
      gl.getExtension('WEBGL_lose_context')?.loseContext()
    }
  } catch {
    webgl2 = false
  }
  let simd = false
  try {
    simd = WebAssembly.validate(SIMD_PROBE)
  } catch {
    simd = false
  }
  return {
    webgl2,
    webgpu: typeof navigator !== 'undefined' && 'gpu' in navigator,
    renderer,
    simd,
    coarse: typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches,
    widthPx: typeof innerWidth === 'number' ? innerWidth : 0,
  }
}

// ── 原生 Viewer（§21.9.4 末段；17 §18.10.3） ─────────────────────────────────────────────────────────────────────────────

/** `python -m awr.bench.rerun.open --run <rid> [--t <s>] [--catalog eval]`（本机代理注入鉴权，命令中不含令牌） */
export function nativeCommand(rid: string, tS: number | null, catalog: CatalogKind = 'dist'): string {
  const t = tS !== null && Number.isFinite(tS) && tS > 0 ? ` --t ${tS.toFixed(1)}` : ''
  return `python -m awr.bench.rerun.open --run ${rid}${t}${catalog === 'eval' ? ' --catalog eval' : ''}`
}

/** 字节 → MB 文本（2^20，一位小数） */
export const fmtMb = (b: number): string => (b / 2 ** 20).toFixed(1)
