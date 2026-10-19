// lab 播放头与 Rerun Web Viewer 的双向时间同步与选择映射（AWR-14 §21.9.3、§21.13；AWR-05 §6.1.6、ADR-149、ADR-173；D3-AC-25）。
// 纯逻辑，不含 React 与网络；Viewer 与播放头经端口注入，测试以假 Viewer 与假时钟驱动。规则：
// - lab 播放头是时间权威，Viewer 为跟随方；三维场景视图或时间线的拖动经 `set_current_time(recording_id, "sim_time", ns)` 推送。
//   拖动中（setScrubbing(true)）推送 ≤ 10 Hz，被节流的最后一个时刻在节流窗口结束与拖动结束时补推（拖动结束补推精确时刻，误差 0）；
//   非拖动时与 Viewer 当前时刻相差小于 1 帧（100 ms 仿真时间）不推送（误差 < 1 帧）；
// - Viewer 中的拖动或播放经 `time_update` 回写播放头：回写期间置防重入标记（播放头订阅因此不回推），并把 Viewer 时刻记为已同步时刻；
//   与已同步时刻相差小于 1 帧的更新合并，在 100 ms 后补写精确时刻（店写入 ≤ 10 Hz，AWR-14 §21.13 规则 1）；
// - 回声：每次推送登记一个待回声值，Viewer 回报的同值时刻（10 s 内）判为回声并丢弃；另按 §21.9.3 伪代码，推送后 50 ms 内与推送值相差
//   小于 1 帧的更新也判为回声（Viewer 对时刻取整的情形）。回声从不回写播放头，因此两侧不会来回触发（100 次往复无回环）；
// - Viewer 播放（`play`）时播放头切换为跟随 Viewer（source = rerun），并暂停本地时钟；Viewer 暂停（`pause`）时补写最后时刻并交回权威；
//   跟随期间用户在场景或时间线上的任何操作（seek 或 play）立即收回权威并暂停 Viewer（§21.9.3 第 2 条）；
// - 只处理 `sim_time` 时间轴：用户在 Viewer 中切到其他时间轴（如 tick）时不回写，attach 时把活动时间轴设为 sim_time；
// - attach 时暂停 Viewer 并推送播放头时刻；在该推送的回声到达前（≤ 10 s）丢弃 Viewer 的时刻与时间轴事件：0.38 打开 .rrd 后即开始播放
//   且不派发 `play`，打开时排队的 time_update 与 timeline_change（缺省时间轴为 tick）会晚于 attach 到达（本机 Chrome 实测，见 R06 记录），
//   不丢弃会把播放头拉回录制起点。Viewer 按派发顺序回报事件，因此回声之前到达的事件都早于 attach。
// 计数（stats）供验收用例与 Playwright `lab/inspect` 读取：pushed（推送次数）= Viewer 收到的 set_current_time 次数；echoes（被丢弃的回声）；
// applied（回写播放头次数）；coalesced（被合并的推送或回写）；reclaimed（收回权威次数）。

/** 唯一的时间轴（AWR-05 §6.1.3：sim_time 为 duration，事件起点起算，纳秒，权威） */
export const SYNC_TIMELINE = 'sim_time'
/** 1 帧（ns）：D3-AC-25"误差 ≤ 1 帧（≤ 100 ms 仿真时间）" */
export const FRAME_NS = 100_000_000
/** 拖动中的推送上限（Hz） */
export const PUSH_HZ = 10
/** 推送后判为回声的时间窗（ms，§21.9.3 伪代码 ECHO_WINDOW_MS） */
export const ECHO_WINDOW_MS = 50
/**
 * 待回声值的保留时长（ms）：超过后 Viewer 仍未回报的推送不再等待回声。Viewer 在渲染下一帧时才应用 set_current_time 并派发回声，
 * 主线程被长帧阻塞时回声会整批迟到（本机软件渲染实测单帧约 0.9 s、首帧约 8 s）；按值精确匹配，保留期长不会误判用户操作。
 */
export const ECHO_KEEP_MS = 10_000

/** 播放头时刻（s）→ Viewer 时刻（ns） */
export const toNs = (s: number): number => Math.round(s * 1e9)

export interface ViewerTimeEvent { recording_id?: string; time: number }
export interface ViewerTimelineEvent { recording_id?: string; timeline: string; time: number }
export interface ViewerPlayEvent { recording_id?: string }

export interface ViewerOpenEvent { recording_id: string; source?: string; version?: string }
export interface ViewerSelectionEvent { recording_id?: string; items: readonly { type: string; entity_path?: string }[] }
/** Viewer 事件（@rerun-io/web-viewer 0.38 ViewerEvent 的子集） */
export interface ViewerEventMap {
  time_update: ViewerTimeEvent
  timeline_change: ViewerTimelineEvent
  play: ViewerPlayEvent
  pause: ViewerPlayEvent
  recording_open: ViewerOpenEvent
  selection_change: ViewerSelectionEvent
}

/** Viewer 一侧的最小接口（@rerun-io/web-viewer WebViewer 的子集，适配见 viewerLoader.ts） */
export interface ViewerPort {
  set_current_time(recordingId: string, timeline: string, timeNs: number): void
  set_playing(recordingId: string, value: boolean): void
  set_active_timeline(recordingId: string, timeline: string): void
  on<K extends keyof ViewerEventMap>(event: K, cb: (e: ViewerEventMap[K]) => void): () => void
}

export interface PlayheadSnap { tS: number; playing: boolean }
/** lab 播放头一侧的最小接口（stores/lab/playhead.ts 的适配见 labPlayheadPort） */
export interface PlayheadPort {
  get(): PlayheadSnap
  /** 跳到 tS（s）；超出当前时长时扩展时长 */
  seek(tS: number): void
  pause(): void
  /** 同步回调（zustand subscribe 语义）：每次写入后以新旧状态调用 */
  subscribe(fn: (s: PlayheadSnap, prev: PlayheadSnap) => void): () => void
}

export interface SyncClock {
  now(): number
  setTimeout(fn: () => void, ms: number): unknown
  clearTimeout(h: unknown): void
}
export const realClock: SyncClock = {
  now: () => performance.now(),
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
}

export type TimeSource = 'lab' | 'rerun'
export interface SyncStats { pushed: number; echoes: number; applied: number; coalesced: number; reclaimed: number; stale: number }

export interface RerunSyncOptions { frameNs?: number; pushHz?: number; echoWindowMs?: number; clock?: SyncClock }

export class RerunSync {
  readonly frameNs: number
  private readonly minPushMs: number
  private readonly echoWindowMs: number
  private readonly clock: SyncClock
  private rec: string | null = null
  private unsubs: (() => void)[] = []
  private applying = false
  private scrubbing = false
  private viewerPlaying = false
  private timeline = SYNC_TIMELINE
  private src: TimeSource = 'lab'
  /** Viewer 当前时刻的最新已知值（推送值或回写值，ns）；-1 为未知 */
  private syncedNs = -1
  private lastPushMs = -Infinity
  private pendingPushNs: number | null = null
  private pushTimer: unknown = null
  private pendingApplyNs: number | null = null
  private applyTimer: unknown = null
  private echoes: { ns: number; atMs: number }[] = []
  /** attach 推送的回声到达前为 true（丢弃 attach 之前排队的 Viewer 事件） */
  private settling = false
  private settleNs = -1
  private settleTimer: unknown = null
  private readonly sourceListeners = new Set<(s: TimeSource) => void>()
  private readonly st: SyncStats = { pushed: 0, echoes: 0, applied: 0, coalesced: 0, reclaimed: 0, stale: 0 }

  constructor(private readonly viewer: ViewerPort, private readonly playhead: PlayheadPort, o: RerunSyncOptions = {}) {
    this.frameNs = o.frameNs ?? FRAME_NS
    this.minPushMs = 1000 / (o.pushHz ?? PUSH_HZ)
    this.echoWindowMs = o.echoWindowMs ?? ECHO_WINDOW_MS
    this.clock = o.clock ?? realClock
  }

  /** 录制已打开（recording_open）：开始双向同步，并把 Viewer 对齐到播放头当前时刻 */
  attach(recordingId: string): void {
    this.detach()
    this.rec = recordingId
    this.viewer.set_playing(recordingId, false)
    this.viewer.set_active_timeline(recordingId, SYNC_TIMELINE)
    this.timeline = SYNC_TIMELINE
    this.unsubs.push(
      this.playhead.subscribe((s, prev) => this.onPlayhead(s, prev)),
      this.viewer.on('time_update', (e) => this.onViewerTime(e)),
      this.viewer.on('timeline_change', (e) => {
        if (this.mine(e) && !this.settling) this.timeline = e.timeline
      }),
      this.viewer.on('play', (e) => this.mine(e) && !this.settling && this.onViewerPlay()),
      this.viewer.on('pause', (e) => this.mine(e) && this.onViewerPause()),
    )
    const t = toNs(this.playhead.get().tS)
    this.settling = true
    this.settleNs = t
    this.settleTimer = this.clock.setTimeout(() => {
      this.settleTimer = null
      this.settling = false
    }, ECHO_KEEP_MS)
    this.push(t, true)
  }

  detach(): void {
    for (const u of this.unsubs.splice(0)) u()
    this.clearTimers()
    this.rec = null
    this.viewerPlaying = false
    this.setSource('lab')
    this.echoes = []
    this.syncedNs = -1
  }

  /** 时间线或场景视图的拖动开始与结束；结束时补推精确时刻 */
  setScrubbing(on: boolean): void {
    if (this.scrubbing === on) return
    this.scrubbing = on
    if (!on && this.rec) {
      if (this.pushTimer !== null) this.clock.clearTimeout(this.pushTimer)
      this.pushTimer = null
      this.pendingPushNs = null
      const t = toNs(this.playhead.get().tS)
      if (t !== this.syncedNs) this.push(t, true)
    }
  }

  get source(): TimeSource {
    return this.src
  }
  get recordingId(): string | null {
    return this.rec
  }
  /** Viewer 当前时刻的最新已知值（ns；未知为 null） */
  get viewerTimeNs(): number | null {
    return this.syncedNs < 0 ? null : this.syncedNs
  }
  stats(): Readonly<SyncStats> {
    return { ...this.st }
  }
  onSource(cb: (s: TimeSource) => void): () => void {
    this.sourceListeners.add(cb)
    return () => {
      this.sourceListeners.delete(cb)
    }
  }

  // ── lab → Viewer ─────────────────────────────────────────────────────────────────────────────────────────────────────

  private onPlayhead(s: PlayheadSnap, prev: PlayheadSnap): void {
    if (this.applying || !this.rec) return
    const moved = s.tS !== prev.tS
    if (this.src === 'rerun' && (moved || (s.playing && !prev.playing))) this.reclaim()
    if (moved) this.push(toNs(s.tS), false)
  }

  private push(tNs: number, force: boolean): void {
    const rec = this.rec
    if (!rec) return
    const now = this.clock.now()
    if (!force && this.scrubbing && now - this.lastPushMs < this.minPushMs) {
      // 拖动中节流：只保留最后一个时刻，节流窗口结束时补推
      this.pendingPushNs = tNs
      this.st.coalesced++
      if (this.pushTimer === null) {
        this.pushTimer = this.clock.setTimeout(() => {
          this.pushTimer = null
          const p = this.pendingPushNs
          this.pendingPushNs = null
          if (p !== null && this.rec) this.push(p, false)
        }, this.minPushMs - (now - this.lastPushMs))
      }
      return
    }
    if (!force && this.syncedNs >= 0 && Math.abs(tNs - this.syncedNs) < this.frameNs) return
    this.viewer.set_current_time(rec, SYNC_TIMELINE, tNs)
    this.st.pushed++
    this.syncedNs = tNs
    this.lastPushMs = now
    this.echoes.push({ ns: tNs, atMs: now })
  }

  private reclaim(): void {
    if (this.rec && this.viewerPlaying) this.viewer.set_playing(this.rec, false)
    this.viewerPlaying = false
    if (this.applyTimer !== null) this.clock.clearTimeout(this.applyTimer)
    this.applyTimer = null
    this.pendingApplyNs = null
    this.st.reclaimed++
    this.setSource('lab')
  }

  // ── Viewer → lab ─────────────────────────────────────────────────────────────────────────────────────────────────────

  private mine(e: { recording_id?: string }): boolean {
    return this.rec !== null && (e.recording_id === undefined || e.recording_id === this.rec)
  }

  private isEcho(ns: number, now: number): boolean {
    this.echoes = this.echoes.filter((x) => now - x.atMs <= ECHO_KEEP_MS)
    const i = this.echoes.findIndex((x) => x.ns === ns)
    if (i >= 0) {
      this.echoes.splice(i, 1)                                        // 只消去该值：回声乱序到达时其余待回声值仍可识别
      return true
    }
    return now - this.lastPushMs < this.echoWindowMs && Math.abs(ns - this.syncedNs) < this.frameNs
  }

  private onViewerTime(e: ViewerTimeEvent): void {
    if (!this.mine(e) || !Number.isFinite(e.time)) return
    const ns = Math.round(e.time)
    const now = this.clock.now()
    if (this.settling) {
      if (ns === this.settleNs && this.isEcho(ns, now)) {
        this.st.echoes++
        this.endSettle()
      } else this.st.stale++
      return
    }
    if (this.timeline !== SYNC_TIMELINE) return
    if (this.isEcho(ns, now)) {
      this.st.echoes++
      return
    }
    if (this.syncedNs >= 0 && Math.abs(ns - this.syncedNs) < this.frameNs) {
      // 小于 1 帧的更新合并，100 ms 后补写精确时刻
      this.pendingApplyNs = ns
      this.st.coalesced++
      if (this.applyTimer === null) {
        this.applyTimer = this.clock.setTimeout(() => {
          this.applyTimer = null
          const p = this.pendingApplyNs
          this.pendingApplyNs = null
          if (p !== null && this.rec) this.apply(p)
        }, this.minPushMs)
      }
      return
    }
    if (this.applyTimer !== null) this.clock.clearTimeout(this.applyTimer)
    this.applyTimer = null
    this.pendingApplyNs = null
    this.apply(ns)
  }

  private apply(ns: number): void {
    this.applying = true
    try {
      this.playhead.seek(ns / 1e9)
    } finally {
      this.applying = false
    }
    this.syncedNs = ns
    this.st.applied++
  }

  private onViewerPlay(): void {
    this.viewerPlaying = true
    if (this.playhead.get().playing) {
      this.applying = true
      try {
        this.playhead.pause()
      } finally {
        this.applying = false
      }
    }
    this.setSource('rerun')
  }

  private onViewerPause(): void {
    this.viewerPlaying = false
    if (this.applyTimer !== null) this.clock.clearTimeout(this.applyTimer)
    this.applyTimer = null
    const p = this.pendingApplyNs
    this.pendingApplyNs = null
    if (p !== null) this.apply(p)
    this.setSource('lab')
  }

  private endSettle(): void {
    this.settling = false
    if (this.settleTimer !== null) this.clock.clearTimeout(this.settleTimer)
    this.settleTimer = null
  }

  private setSource(s: TimeSource): void {
    if (this.src === s) return
    this.src = s
    for (const cb of this.sourceListeners) cb(s)
  }

  private clearTimers(): void {
    this.endSettle()
    if (this.pushTimer !== null) this.clock.clearTimeout(this.pushTimer)
    if (this.applyTimer !== null) this.clock.clearTimeout(this.applyTimer)
    this.pushTimer = null
    this.applyTimer = null
    this.pendingPushNs = null
    this.pendingApplyNs = null
  }
}

// ── 选择联动（§21.9.3 第 3 条） ────────────────────────────────────────────────────────────────────────────────────────

export type LabSelection = { kind: 'uav'; id: string } | { kind: 'actor'; id: string }

const FLEET_RE = /^\/(?:truth\/)?fleet\/([a-z0-9_-]+)(?:\/|$)/
const ACTOR_RE = /^\/truth\/actors\/([a-z0-9_-]+)(?:\/|$)/

/** Viewer 选中的实体路径 → 场景视图对象：`/fleet/<uav>/**` 与 `/truth/fleet/<uav>/**` 为机体，`/truth/actors/<id>/**` 为真值实体 */
export function entityToSelection(path: string): LabSelection | null {
  const f = FLEET_RE.exec(path)
  if (f) return { kind: 'uav', id: f[1]! }
  const a = ACTOR_RE.exec(path)
  return a ? { kind: 'actor', id: a[1]! } : null
}

/** selection_change 的条目列表 → 第一个可映射的实体 */
export function selectionFromItems(items: readonly { type: string; entity_path?: string }[]): LabSelection | null {
  for (const it of items) {
    if (it.type !== 'entity' || typeof it.entity_path !== 'string') continue
    const s = entityToSelection(it.entity_path)
    if (s) return s
  }
  return null
}

/** 场景视图对象 → Viewer 实体路径；真值实体只在可读真值的会话（评测 catalog）中可定位 */
export function selectionEntity(sel: LabSelection, truth: boolean): string | null {
  if (!/^[a-z0-9_-]+$/.test(sel.id)) return null
  if (sel.kind === 'uav') return `/fleet/${sel.id}`
  return truth ? `/truth/actors/${sel.id}` : null
}

/** 在 Viewer URL 的片段中设置 `selection=<实体路径>`，保留已有的 `when`、`time_selection` 等片段参数 */
export function withSelectionFragment(url: string, entity: string): string {
  const i = url.indexOf('#')
  const base = i < 0 ? url : url.slice(0, i)
  const parts = (i < 0 ? '' : url.slice(i + 1)).split('&').filter((p) => p && !p.startsWith('selection='))
  return `${base}#${[...parts, `selection=${entity}`].join('&')}`
}
