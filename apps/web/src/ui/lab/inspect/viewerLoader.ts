// Rerun Web Viewer 的按需加载与启动（AWR-05 §6.1.5、§6.1.6；AWR-14 §21.9.2 LOADING；D3-AC-25"工作区按需加载 Web Viewer""Viewer
// 未加载时 D1-AC-03b 不回退"）。
// - 版本锁定：npm 包 `@rerun-io/web-viewer` 0.38.1，与 rerun-sdk、catalog 服务同一版本（apps/web/package.json 精确版本）；
// - 按需加载：本文件是 src 中唯一引用该包的模块，且只以动态 import() 引用；进入数据检视页并开始加载前，包的 JS 分块与 Wasm（51.2 MB，
//   自托管，构建产物文件名带内容哈希、可长期缓存）都不进入页面，三维沙盘与其他页面的入口分块不含 Viewer 代码；
// - 加载进度：Viewer 自带的英文加载遮罩被宿主外壳覆盖，进度由宿主按 Wasm 已收字节显示（包装全局 fetch，只在 start 期间、只对
//   re_viewer_bg*.wasm 计数；总量取锁定版本的未压缩字节数，压缩传输时按解压后字节计数）；
// - 配置：hide_welcome_screen、check_for_updates_on_startup = false、memory_limit = 1.5GB（≤ 1.5 GB，与 awr.bench.catalog.query
//   VIEWER_MEMORY_LIMIT 相同）、主题跟随宿主；以 override_panel_state 隐藏 Rerun 顶栏与选择面板；不启用全屏与历史记录；
// - 释放：stop() 释放 Wasm 内存与画布（路由离开时调用）。
import type { ViewerEventMap, ViewerPort } from './rerunSync'

/** 锁定版本（AWR-05 §6.1.5；与 requirements-d3.in 的 rerun-sdk==0.38.1 相同） */
export const RERUN_VIEWER_VERSION = '0.38.1'
/** 锁定版本 re_viewer_bg.wasm 的未压缩字节数（进度总量；测试核对 node_modules 中的文件） */
export const VIEWER_WASM_BYTES = 51_225_405
/** Web Viewer 内存上限（D3-AC-25 `memory_limit` ≤ 1.5 GB） */
export const VIEWER_MEMORY_LIMIT = '1.5GB'
export const VIEWER_MEMORY_LIMIT_MAX = 1_500_000_000

export type ViewerPanel = 'top' | 'blueprint' | 'selection' | 'time'
/** 宿主隐藏的 Rerun 面板（§6.1.6 配置行：顶栏与选择面板）；时间面板保留，供 Viewer 内拖动 */
export const HIDDEN_PANELS: readonly ViewerPanel[] = ['top', 'selection']

/** 宿主使用的 Viewer 接口：同步端口加打开、关闭、停止与面板控制 */
export interface ViewerHandle extends ViewerPort {
  open(rrd: string | string[]): void
  close(rrd: string | string[]): void
  stop(): void
  override_panel_state(panel: ViewerPanel, state: 'hidden' | 'collapsed' | 'expanded' | null): void
  get_time_range(recordingId: string, timeline: string): { min: number; max: number } | null
  get_current_time(recordingId: string, timeline: string): number
}

/** @rerun-io/web-viewer 的 WebViewer 实例（只声明宿主用到的成员；类型由包的 index.d.ts 保证兼容） */
interface WebViewerLike {
  start(rrd: string | string[] | null, parent: HTMLElement | null, options: Record<string, unknown> | null): Promise<void>
  on(event: string, cb: (e: never) => void): () => void
  open(rrd: string | string[]): void
  close(rrd: string | string[]): void
  stop(): void
  override_panel_state(panel: ViewerPanel, state: 'hidden' | 'collapsed' | 'expanded' | null | undefined): void
  set_current_time(recordingId: string, timeline: string, time: number): void
  get_current_time(recordingId: string, timeline: string): number
  set_playing(recordingId: string, value: boolean): void
  set_active_timeline(recordingId: string, timeline: string): void
  get_time_range(recordingId: string, timeline: string): { min: number; max: number } | null
}
export interface ViewerModule { WebViewer: new () => WebViewerLike }
export type ViewerModuleLoader = () => Promise<ViewerModule>

/** 唯一的引用点：动态 import()，构建为独立分块 */
export const importViewer: ViewerModuleLoader = () => import('@rerun-io/web-viewer') as unknown as Promise<ViewerModule>

export type HostTheme = 'dark' | 'light'
/** 宿主主题（app/providers/ThemeProvider 在 <html> 上切换 .dark） */
export function hostTheme(): HostTheme {
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

/** WebViewer.start 的配置（未知键原样传给 Wasm 侧的 AppOptions：memory_limit、check_for_updates_on_startup） */
export function viewerStartOptions(theme: HostTheme): Record<string, unknown> {
  return {
    hide_welcome_screen: true,
    check_for_updates_on_startup: false,
    memory_limit: VIEWER_MEMORY_LIMIT,
    allow_fullscreen: false,
    enable_history: false,
    theme,
    width: '100%',
    height: '100%',
  }
}

const UNITS: Readonly<Record<string, number>> = { B: 1, KB: 1e3, MB: 1e6, GB: 1e9, KIB: 2 ** 10, MIB: 2 ** 20, GIB: 2 ** 30 }
/** "1.5GB" → 1.5e9（与 Rerun 的 memory_limit 解析一致：GB 为 10^9，GiB 为 2^30）；不可解析为 NaN */
export function parseMemoryLimit(s: string): number {
  const m = /^\s*([\d.]+)\s*([a-z]*)\s*$/i.exec(s)
  if (!m) return Number.NaN
  const u = UNITS[(m[2] || 'B').toUpperCase()]
  return u === undefined ? Number.NaN : Number(m[1]) * u
}

const WASM_RE = /re_viewer_bg[^/?#]*\.wasm(?:[?#]|$)/
type Fetch = typeof globalThis.fetch

/** 计数包装：fn 执行期间对 Viewer Wasm 的响应按已收字节回调；给出 wasmUrl 时把对 Viewer Wasm 的请求改到该地址（ADR-221：
 * ModelScope 副本，CORS 模式）；fn 结束后恢复原 fetch */
export async function withWasmProgress<T>(onBytes: (received: number) => void, fn: () => Promise<T>, wasmUrl?: string | null): Promise<T> {
  const orig: Fetch = globalThis.fetch
  const wrapped: Fetch = async (input, init) => {
    const asked = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const redirect = !!wasmUrl && WASM_RE.test(asked)
    const r = await orig(redirect ? wasmUrl : input, redirect ? { ...init, mode: 'cors', credentials: 'omit' } : init)
    const url = redirect ? wasmUrl : asked
    if (!WASM_RE.test(url) || !r.body) return r
    let got = 0
    const reader = r.body.getReader()
    const body = new ReadableStream<Uint8Array>({
      async pull(c) {
        const { done, value } = await reader.read()
        if (done) {
          c.close()
          return
        }
        got += value.byteLength
        onBytes(got)
        c.enqueue(value)
      },
      cancel(reason) {
        return reader.cancel(reason)
      },
    })
    return new Response(body, { status: r.status, statusText: r.statusText, headers: r.headers })
  }
  globalThis.fetch = wrapped
  try {
    return await fn()
  } finally {
    if (globalThis.fetch === wrapped) globalThis.fetch = orig
  }
}

/** 适配：WebViewer 实例 → ViewerHandle（事件名与载荷按 0.38 的 ViewerEvent） */
export function adaptViewer(wv: WebViewerLike): ViewerHandle {
  return {
    set_current_time: (r, tl, t) => wv.set_current_time(r, tl, t),
    set_playing: (r, v) => wv.set_playing(r, v),
    set_active_timeline: (r, tl) => wv.set_active_timeline(r, tl),
    on: <K extends keyof ViewerEventMap>(ev: K, cb: (e: ViewerEventMap[K]) => void) => wv.on(ev, cb as (e: never) => void),
    open: (rrd) => wv.open(rrd),
    close: (rrd) => wv.close(rrd),
    stop: () => wv.stop(),
    override_panel_state: (p, s) => wv.override_panel_state(p, s),
    get_time_range: (r, tl) => wv.get_time_range(r, tl),
    get_current_time: (r, tl) => wv.get_current_time(r, tl),
  }
}

export interface StartViewerOptions {
  theme?: HostTheme
  /** Wasm 已收字节 */
  onBytes?: (received: number) => void
  /** Viewer Wasm 的替代地址（ADR-221：ModelScope 副本）；缺省自托管 */
  wasmUrl?: string | null
  load?: ViewerModuleLoader
}

/** 加载包并在 parent 中启动 Viewer（不打开录制）；失败时已释放 */
export async function startViewer(parent: HTMLElement, o: StartViewerOptions = {}): Promise<ViewerHandle> {
  const mod = await (o.load ?? importViewer)()
  const wv = new mod.WebViewer()
  try {
    await withWasmProgress(o.onBytes ?? (() => {}), () => wv.start(null, parent, viewerStartOptions(o.theme ?? hostTheme())), o.wasmUrl)
  } catch (e) {
    try {
      wv.stop()
    } catch {
      // 已停止
    }
    throw e
  }
  for (const p of HIDDEN_PANELS) wv.override_panel_state(p, 'hidden')
  return adaptViewer(wv)
}
