// 数据检视面板的加载状态机（AWR-14 §21.9.2）与同步装配（§21.9.3）。不含 React：面板组件订阅快照，测试以假 Viewer 驱动。
//   UNSUPPORTED  设备档不允许（Tier S、无 GPU 接口、无 Wasm SIMD、手机与平板布局）；终态
//   IDLE         外壳与"加载数据检视"；首次进入自动开始（autoStart），用户关闭后不再自动
//   LOADING      动态加载 @rerun-io/web-viewer 分块与 Wasm（按已收字节显示进度）；成功 → OPENING，失败 → ERROR(load)
//   OPENING      内部：BN-57 viewer_url（设置 d3_rr Cookie）后 open(url)；公开站：open(切片…, basemap.rrd)，不发起任何 catalog 请求；
//                对应 recording_id 的 recording_open → READY；10 s 无事件 → ERROR(timeout)；录制版本跨 minor → ERROR(version, 694)
//   READY        Viewer 可交互；RerunSync 双向同步与选择联动生效
//   ERROR        重试 → 已加载 Viewer 时回到 OPENING，否则回到 LOADING
// 卸载或关闭时 stop() 释放 Wasm 内存与画布；在途的加载与请求以代号作废，不再改变状态。
import {
  checkLink, checkStaticSource, errorOf, OPEN_TIMEOUT_MS, sameMinor, staticUrls, type InspectError, type InspectSource, type Support,
  type ViewerLink,
} from './inspect'
import {
  RerunSync, realClock, selectionEntity, selectionFromItems, withSelectionFragment, type LabSelection, type PlayheadPort, type SyncClock,
  type TimeSource,
} from './rerunSync'
import { startViewer, VIEWER_WASM_BYTES, type StartViewerOptions, type ViewerHandle } from './viewerLoader'

export type InspectState = 'UNSUPPORTED' | 'IDLE' | 'LOADING' | 'OPENING' | 'READY' | 'ERROR'

export interface InspectSnapshot {
  state: InspectState
  receivedBytes: number
  totalBytes: number
  error: InspectError | null
  recordingId: string | null
  link: ViewerLink | null
  source: TimeSource
  autoStart: boolean
  unsupported: Support | null
}

export interface InspectDeps {
  source: InspectSource
  playhead: PlayheadPort
  support: Support
  start?: (parent: HTMLElement, o: StartViewerOptions) => Promise<ViewerHandle>
  clock?: SyncClock
  openTimeoutMs?: number
  /** 录制的 sim_time 区间（s），READY 时回调一次（页面据此设置播放头时长） */
  onRange?: (minS: number, maxS: number) => void
  /** Viewer 中的选择映射到场景对象 */
  onSelect?: (sel: LabSelection) => void
  /** 把相对地址解析为本站绝对地址（缺省以 location.href 为基准） */
  resolveUrl?: (u: string) => string
}

const absolute = (u: string): string => (typeof location === 'undefined' ? u : new URL(u, location.href).href)

export class InspectController {
  private snap: InspectSnapshot
  private readonly listeners = new Set<() => void>()
  private viewer: ViewerHandle | null = null
  private sync: RerunSync | null = null
  private gen = 0
  private abort: AbortController | null = null
  private openTimer: unknown = null
  private viewerUnsubs: (() => void)[] = []
  private parent: HTMLElement | null = null
  /** 已交给 Viewer 打开的地址（重试前关闭，使 recording_open 重新触发） */
  private opened: string[] = []
  private readonly clock: SyncClock

  constructor(private readonly d: InspectDeps) {
    this.clock = d.clock ?? realClock
    this.snap = {
      state: d.support.ok ? 'IDLE' : 'UNSUPPORTED', receivedBytes: 0, totalBytes: VIEWER_WASM_BYTES, error: null, recordingId: null, link: null,
      source: 'lab', autoStart: true, unsupported: d.support.ok ? null : d.support,
    }
  }

  get snapshot(): InspectSnapshot {
    return this.snap
  }
  /** 当前的同步器（READY 后存在；测试与 Playwright 读取 stats） */
  get rerunSync(): RerunSync | null {
    return this.sync
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn)
    return () => {
      this.listeners.delete(fn)
    }
  }

  private set(p: Partial<InspectSnapshot>): void {
    this.snap = { ...this.snap, ...p }
    for (const l of this.listeners) l()
  }

  /** IDLE 或 ERROR → 加载（已加载 Viewer 时直接打开） */
  async start(parent: HTMLElement): Promise<void> {
    if (this.snap.state !== 'IDLE' && this.snap.state !== 'ERROR') return
    this.parent = parent
    const g = ++this.gen
    if (!this.viewer) {
      this.set({ state: 'LOADING', error: null, receivedBytes: 0 })
      try {
        const v = await (this.d.start ?? startViewer)(parent, {
          onBytes: (n) => g === this.gen && this.set({ receivedBytes: Math.min(n, this.snap.totalBytes) }),
        })
        if (g !== this.gen) {
          v.stop()
          return
        }
        this.viewer = v
        this.set({ receivedBytes: this.snap.totalBytes })
      } catch (e) {
        if (g === this.gen) this.set({ state: 'ERROR', error: { kind: 'load', detail: e instanceof Error ? e.message : String(e) } })
        return
      }
    }
    await this.open(g)
  }

  private async open(g: number): Promise<void> {
    const v = this.viewer!
    const src = this.d.source
    this.set({ state: 'OPENING', error: null })
    const resolve = this.d.resolveUrl ?? absolute
    let recId: string
    let urls: string[]
    let link: ViewerLink | null = null
    if (src.kind === 'static') {
      const bad = checkStaticSource(src)
      if (bad) return this.fail(g, bad)
      recId = src.recordingId
      urls = staticUrls(src).map(resolve)
    } else {
      this.abort?.abort()
      const ac = new AbortController()
      this.abort = ac
      try {
        link = await src.fetchLink(ac.signal)
      } catch (e) {
        return this.fail(g, errorOf(e))
      }
      if (g !== this.gen) return
      const bad = checkLink(link, { catalog: src.catalog })
      if (bad) return this.fail(g, bad)
      recId = link.recording_id
      urls = [link.url]
    }
    this.unbindViewer()
    this.viewerUnsubs.push(
      v.on('recording_open', (e) => {
        if (g !== this.gen || e.recording_id !== recId || this.snap.state !== 'OPENING') return
        // catalog 来源（redap）的 recording_open 不带写出版本（0.38.1 实测为 null），版本已由 BN-57 的 rerun_version 校验
        if (e.version != null && !sameMinor(e.version)) return this.fail(g, { kind: 'version', detail: e.version })
        this.ready(g, recId)
      }),
      v.on('selection_change', (e) => {
        if (g !== this.gen || (e.recording_id !== undefined && e.recording_id !== recId)) return
        const sel = selectionFromItems(e.items ?? [])
        if (sel) this.d.onSelect?.(sel)
      }),
    )
    if (this.opened.length) v.close(this.opened.length === 1 ? this.opened[0]! : this.opened)
    this.opened = urls
    this.set({ link, recordingId: recId })
    this.openTimer = this.clock.setTimeout(() => {
      this.openTimer = null
      if (g === this.gen && this.snap.state === 'OPENING') this.fail(g, { kind: 'timeout', detail: null })
    }, this.d.openTimeoutMs ?? OPEN_TIMEOUT_MS)
    v.open(urls.length === 1 ? urls[0]! : urls)
  }

  private ready(g: number, recId: string): void {
    if (this.openTimer !== null) this.clock.clearTimeout(this.openTimer)
    this.openTimer = null
    const v = this.viewer!
    const r = v.get_time_range(recId, 'sim_time')
    if (r && this.d.onRange) this.d.onRange(r.min / 1e9, r.max / 1e9)
    const sync = new RerunSync(v, this.d.playhead, { clock: this.clock })
    sync.onSource((s) => g === this.gen && this.set({ source: s }))
    sync.attach(recId)
    this.sync = sync
    this.set({ state: 'READY', error: null })
  }

  private fail(g: number, error: InspectError): void {
    if (g !== this.gen) return
    if (this.openTimer !== null) this.clock.clearTimeout(this.openTimer)
    this.openTimer = null
    this.sync?.detach()
    this.sync = null
    this.set({ state: 'ERROR', error, source: 'lab' })
  }

  /** ERROR → 重试 */
  retry(): Promise<void> {
    return this.parent ? this.start(this.parent) : Promise.resolve()
  }

  /** 时间线或场景视图的拖动（转给同步器） */
  setScrubbing(on: boolean): void {
    this.sync?.setScrubbing(on)
  }

  /**
   * 场景视图中的选择定位到 Viewer（§21.9.3 第 3 条：经 Viewer URL 片段 `#selection=<实体>`）。只对 catalog 来源生效：静态切片按文件
   * 地址打开，重新打开会重新下载，因此公开站只做 Viewer → 场景方向。返回是否已定位。
   */
  selectInViewer(sel: LabSelection, truth: boolean): boolean {
    const link = this.snap.link
    if (this.snap.state !== 'READY' || !this.viewer || !link) return false
    const ent = selectionEntity(sel, truth)
    if (!ent) return false
    this.viewer.open(withSelectionFragment(link.url, ent))
    return true
  }

  /** 用户关闭面板：释放 Viewer，回到 IDLE，之后不再自动加载 */
  close(): void {
    this.teardown()
    if (this.snap.state !== 'UNSUPPORTED') this.set({ state: 'IDLE', autoStart: false, error: null, link: null, recordingId: null, source: 'lab', receivedBytes: 0 })
  }

  /**
   * 卸载（路由离开）：释放 Viewer 与 Wasm 内存，在途的加载作废。状态回到 IDLE 且保留 autoStart，使开发构建 StrictMode 的
   * "卸载后立即重新挂载"仍能自动加载；订阅由订阅方各自退订。
   */
  dispose(): void {
    this.teardown()
    if (this.snap.state !== 'UNSUPPORTED' && this.snap.state !== 'IDLE') {
      this.set({ state: 'IDLE', error: null, link: null, recordingId: null, source: 'lab', receivedBytes: 0 })
    }
  }

  private unbindViewer(): void {
    for (const u of this.viewerUnsubs.splice(0)) u()
  }

  private teardown(): void {
    this.gen++
    this.abort?.abort()
    this.abort = null
    if (this.openTimer !== null) this.clock.clearTimeout(this.openTimer)
    this.openTimer = null
    this.sync?.detach()
    this.sync = null
    this.unbindViewer()
    const v = this.viewer
    this.viewer = null
    this.opened = []
    if (v) {
      try {
        v.stop()
      } catch {
        // 已停止
      }
    }
  }
}
