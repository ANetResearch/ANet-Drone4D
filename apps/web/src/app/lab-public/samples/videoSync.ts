// 多机私有视角视频与 lab 播放头的同步（AWR-14 §21.29.3 规则 3、§21.7.4 公开站行、§21.13 规则 2；D3-AC-21"RGB 与热成像伪彩视频按
// 仿真时间同步，偏差 ≤ 1 帧"；UX-AC-113）。不含 React：页面每个动画帧调用一次 tick()，测试以假视频元素驱动。Owner: M15.
// 时间模型：
// - 视角视频由存档帧转码（M27-FR-135：640 × 360 @ 10 Hz，H.264），帧 k 的显示时间为视频时间 [k / fps, (k + 1) / fps)；视频时间 v
//   对应仿真时刻 t0S + v（t0S 为首帧的运行仿真时刻，缺省 0：样例运行为 Env 运行，事件偏移为 0）；
// - 播放头 tS 应显示的帧 k = ⌊(tS − t0S) · fps⌋（不取未来帧，与观测墙取帧规则相同）；偏差 = 已显示帧号 − k（帧）。
// 控制（以 lab 播放头为准）：
// 1. 暂停或拖动：视频暂停，所显示帧不是 k 时 seek 到帧 k 的中点（避开帧边界的取整误差）；
// 2. 播放：视频自行播放，速率 = 播放头速率，视频时钟目标为播放头减半帧（PLAY_LAG_FRAMES，抵消合成器提前半帧选帧）；
//    漂移 |Δ| > 1 帧时重新 seek（§21.29.3 规则 3），0.25–1 帧之间以 ±10% 以内的速率微调
//    收敛（微调量 = 漂移 / 0.25 s，至多 ±20%），避免频繁 seek 造成停顿；播放中的 seek 以实测 seek 时延（指数平均，至多 250 ms）
//    前置目标，使视频在 seek 完成时落在播放头上；同一路两次 seek 至少间隔 500 ms；起播时播放头保持到每路视频实际开始推进（至多
//    1 s），使起播延迟不计入偏差；
// 3. 缓冲：播放中任一可见视频未就绪（readyState < HAVE_FUTURE_DATA 或 seeking）超过 500 ms 时进入缓冲，播放头冻结、视频暂停，
//    全部就绪后自动恢复（§21.13 规则 2）；出错的视频（MediaError）不参与缓冲判定；
// 4. 播放头在视频覆盖区间之外：视频暂停并停在首帧或末帧，格内显示"无该时段画面"。
// 度量：每次视频合成新帧（requestVideoFrameCallback 的 mediaTime）时记录已显示帧号与当时播放头应显示帧号之差，stats() 给出最大
// 绝对偏差（帧）、seek 与速率微调次数，供页面数据属性与 Playwright `lab/public-samples` 读取。

/** 视角视频的缺省帧率（M27-FR-135，Hz） */
export const VIDEO_FPS = 10
/** 播放中未就绪超过该时长进入缓冲（ms，§21.13 规则 2） */
export const BUFFERING_MS = 500
/** 速率微调的上限（相对播放头速率） */
export const NUDGE_MAX = 0.2
/** 速率微调的收敛时间常数（s）：微调量 = 漂移 / τ */
export const NUDGE_TAU_S = 0.25
/** 起播后等待视频开始推进的最长时间（ms），其间播放头保持 */
export const HOLD_MAX_MS = 1000
/** seek 时延的初值（ms）与指数平均系数 */
export const SEEK_LATENCY_MS = 60
const SEEK_EWMA = 0.3
/** seek 目标前置量的上限（ms，按 seek 时延计；时延异常大时不过度前置） */
export const SEEK_LEAD_MAX_MS = 250
/** 同一路视频两次播放中 seek 的最短间隔（ms）：间隔内只做速率微调，避免低帧率或慢解码设备上反复 seek 造成停顿 */
export const RESEEK_COOLDOWN_MS = 500
/** 小于该偏差（帧）不微调 */
export const NUDGE_DEADBAND_FRAMES = 0.25
/**
 * 播放中视频时钟相对播放头的滞后量（帧）。浏览器合成器在每次刷新时选择显示区间与刷新区间重叠最多的帧（Chrome
 * VideoRendererAlgorithm），播放中所显示的帧号约为 round(currentTime · fps)，比 ⌊currentTime · fps⌋ 早半帧；视频时钟滞后半帧使
 * 所显示的帧回到"不取未来帧"的 ⌊(tS − t0S) · fps⌋（本机 Chrome 151 实测：不滞后时偏差分布以 +0.5 帧为中心）。暂停时显示 currentTime
 * 所在的帧，不需要滞后（暂停定位到帧中点）。
 */
export const PLAY_LAG_FRAMES = 0.5
/** HTMLMediaElement.readyState */
export const HAVE_CURRENT_DATA = 2
export const HAVE_FUTURE_DATA = 3
const EPS = 1e-6

export interface VideoTiming {
  /** 首帧的运行仿真时刻（s） */
  t0S: number
  fps: number
}

/** 同步器使用的视频元素接口（HTMLVideoElement 的子集） */
export interface VideoLike {
  currentTime: number
  readonly duration: number
  readonly paused: boolean
  playbackRate: number
  readonly readyState: number
  readonly seeking: boolean
  readonly error?: unknown
  play(): Promise<void> | void
  pause(): void
}

export interface ClockState {
  tS: number
  playing: boolean
  rate: number
}

/** 视频时间 vS 处显示的帧号 */
export const frameOfVideo = (vS: number, fps: number): number => Math.floor(vS * fps + EPS)
/** 播放头 tS 应显示的帧号（可为负：首帧之前） */
export const targetFrame = (tS: number, tm: VideoTiming): number => Math.floor((tS - tm.t0S) * tm.fps + EPS)
/** 视频总帧数（时长未知时为 null） */
export const frameCount = (durationS: number, fps: number): number | null =>
  Number.isFinite(durationS) && durationS > 0 ? Math.max(1, Math.round(durationS * fps)) : null
/** 帧 k 的 seek 目标：帧显示区间的中点 */
export const seekTarget = (k: number, fps: number): number => (k + 0.5) / fps

export interface VideoStat {
  id: string
  /** 记录到的合成帧数 */
  presented: number
  /** 合成帧与播放头应显示帧之差的最大绝对值（帧） */
  maxAbsDevFrames: number
  lastDevFrames: number | null
  /** 偏差分布：偏差（帧，截断到 ±9）→ 合成帧数 */
  hist: Record<number, number>
  /** 播放中因漂移 > 1 帧的 seek 次数 */
  reseeks: number
  /** 暂停与拖动时的定位 seek 次数 */
  aligns: number
  nudges: number
  /** 当前是否在视频覆盖区间之外 */
  outside: boolean
}
export interface SyncStats {
  videos: VideoStat[]
  presented: number
  maxAbsDevFrames: number | null
  /** 全部视频合并的偏差分布 */
  hist: Record<number, number>
  reseeks: number
  buffering: boolean
}

interface Entry {
  el: VideoLike
  tm: VideoTiming
  stat: VideoStat
  notReadySince: number | null
  /** 已请求起播、尚未开始推进：{ 开始时刻（ms），当时的视频时间（s）} */
  pending: { since: number; fromS: number } | null
  /** 进行中的 seek 的开始时刻（ms） */
  seekAt: number | null
  /** seek 时延的指数平均（ms） */
  seekMs: number
  /** 最近一次播放中 seek 的时刻（ms） */
  reseekAt: number
  /** 是否已与播放头对齐过一次（新挂载的视频在首次对齐前合成的帧不计偏差） */
  synced: boolean
}

export class VideoSyncGroup {
  private readonly entries = new Map<string, Entry>()
  private buffering = false
  private holding = false
  /** 最近一次 tick 的播放头锚点：时刻（ms，与 performance.now 同一时基）、播放头（s）、是否推进、速率 */
  private anchor = { at: 0, tS: 0, advancing: false, rate: 1 }
  private readonly now: () => number

  constructor(opts: { now?: () => number } = {}) {
    this.now = opts.now ?? (() => (typeof performance !== 'undefined' ? performance.now() : Date.now()))
  }

  /** 登记一路视频；返回注销函数 */
  add(id: string, el: VideoLike, tm: VideoTiming): () => void {
    const stat: VideoStat = { id, presented: 0, maxAbsDevFrames: 0, lastDevFrames: null, hist: {}, reseeks: 0, aligns: 0, nudges: 0, outside: false }
    const e: Entry = {
      el, tm: { t0S: tm.t0S, fps: tm.fps > 0 ? tm.fps : VIDEO_FPS }, stat, notReadySince: null, pending: null, seekAt: null, seekMs: SEEK_LATENCY_MS,
      reseekAt: Number.NEGATIVE_INFINITY, synced: false,
    }
    this.entries.set(id, e)
    return () => {
      if (this.entries.get(id) === e) this.entries.delete(id)
    }
  }

  get size(): number {
    return this.entries.size
  }

  /** 当前是否缓冲中（播放头冻结，界面显示"缓冲中"） */
  get isBuffering(): boolean {
    return this.buffering
  }

  /** 是否在等待视频起播或 seek 完成（播放头保持，不提示） */
  get isHolding(): boolean {
    return this.holding
  }

  /** 播放头本帧是否应推进 */
  get canAdvance(): boolean {
    return !this.buffering && !this.holding
  }

  /**
   * 每个动画帧调用一次：按播放头状态驱动全部视频；返回是否缓冲中。atMs 为播放头 tS 对应的时刻（页面传动画帧时间戳；缺省为调用时刻）：
   * 低帧率页面（Tier S 软件渲染约 5 帧/s）中，视频的 currentTime 在动画帧之间继续推进，漂移按调用时刻外推的播放头计算。
   */
  tick(s: ClockState, atMs?: number): boolean {
    const t = this.now()
    const at = atMs ?? t
    // 先判定缓冲（以本次调用前的视频状态），缓冲中按暂停处理全部视频
    let waiting = false
    for (const e of this.entries.values()) {
      const n = frameCount(e.el.duration, e.tm.fps)
      const k = targetFrame(s.tS, e.tm)
      const inRange = n === null || (k >= 0 && k < n)
      const notReady = !e.el.error && inRange && (e.el.readyState < HAVE_FUTURE_DATA || e.el.seeking)
      if (s.playing && notReady) {
        e.notReadySince ??= t
        if (t - e.notReadySince > BUFFERING_MS) waiting = true
      } else {
        e.notReadySince = null
      }
    }
    // 已在缓冲中时，任一视频仍未就绪就保持缓冲
    if (this.buffering && s.playing) {
      waiting = waiting || [...this.entries.values()].some((e) => e.notReadySince !== null)
    }
    this.buffering = s.playing && waiting
    const playing = s.playing && !this.buffering
    // 视频时钟在本次调用时刻的播放头（推进中按速率外推）
    const tNow = playing && !this.holding ? s.tS + (Math.max(0, t - at) / 1000) * s.rate : s.tS
    let hold = false
    for (const e of this.entries.values()) {
      if (e.seekAt !== null && !e.el.seeking) {
        e.seekMs += SEEK_EWMA * (Math.min(1000, t - e.seekAt) - e.seekMs)
        e.seekAt = null
      }
      this.drive(e, tNow, playing, s.rate)
      if (e.pending) {
        const el = e.el
        const started = !el.seeking && !el.paused && Math.abs(el.currentTime - e.pending.fromS) > EPS
        if (!playing || started || e.stat.outside || el.error || t - e.pending.since > HOLD_MAX_MS) e.pending = null
        else hold = true
      }
    }
    this.holding = hold
    this.anchor = { at, tS: s.tS, advancing: playing && !hold, rate: s.rate }
    return this.buffering
  }

  /** 时刻 ms（performance.now 时基）的播放头估计：推进中按最近一次 tick 的锚点与速率外推，否则为锚点值 */
  playheadAt(ms: number): number {
    const a = this.anchor
    return a.advancing ? a.tS + (Math.max(0, ms - a.at) / 1000) * a.rate : a.tS
  }

  /** 视频合成了一帧：mediaTime（s）在 displayMs（requestVideoFrameCallback 的 expectedDisplayTime）显示，与该时刻的播放头比较 */
  presentedAt(id: string, mediaTimeS: number, displayMs: number): void {
    this.presented(id, mediaTimeS, this.playheadAt(displayMs))
  }

  private drive(e: Entry, tS: number, playing: boolean, rate: number): void {
    const { el, tm, stat } = e
    if (el.error || el.readyState < 1) return // 无元数据：等待加载
    const n = frameCount(el.duration, tm.fps)
    const k0 = targetFrame(tS, tm)
    const outside = n !== null && (k0 < 0 || k0 >= n)
    stat.outside = outside
    const k = n === null ? Math.max(0, k0) : Math.min(Math.max(0, k0), n - 1)
    if (!playing || outside) {
      if (!el.paused) el.pause()
      if (!el.seeking && frameOfVideo(el.currentTime, tm.fps) !== k) {
        el.currentTime = seekTarget(k, tm.fps)
        e.seekAt = this.now()
        stat.aligns++
      } else if (!el.seeking) {
        e.synced = true
      }
      if (el.playbackRate !== rate && rate > 0) el.playbackRate = rate
      return
    }
    if (el.seeking) return
    if (e.pending) return // 起播尚未推进：播放头保持
    if (!e.synced && Math.abs(el.currentTime + tm.t0S - tS) * tm.fps <= 1) e.synced = true
    const driftS = el.currentTime + tm.t0S - (tS - PLAY_LAG_FRAMES / tm.fps)
    const driftFrames = driftS * tm.fps
    const t = this.now()
    if (Math.abs(driftFrames) > 1 && t - e.reseekAt >= RESEEK_COOLDOWN_MS) {
      // 目标前置 seek 时延内播放头将走过的仿真时间
      el.currentTime = Math.max(0, tS - tm.t0S - PLAY_LAG_FRAMES / tm.fps + (rate * Math.min(e.seekMs, SEEK_LEAD_MAX_MS)) / 1000)
      e.seekAt = t
      e.reseekAt = t
      stat.reseeks++
    } else {
      const want = Math.abs(driftFrames) > NUDGE_DEADBAND_FRAMES
        ? Math.min(rate * (1 + NUDGE_MAX), Math.max(rate * (1 - NUDGE_MAX), rate - driftS / NUDGE_TAU_S))
        : rate
      if (Math.abs(el.playbackRate - want) > 1e-3) {
        if (want !== rate) stat.nudges++
        el.playbackRate = want
      }
    }
    if (el.paused) {
      e.pending ??= { since: this.now(), fromS: el.currentTime }
      const p = el.play()
      if (p && typeof (p as Promise<void>).catch === 'function') (p as Promise<void>).catch(() => undefined)
    }
  }

  /** 视频合成了一帧（requestVideoFrameCallback 的 mediaTime，s）；tS 为此刻播放头时刻 */
  presented(id: string, mediaTimeS: number, tS: number): void {
    const e = this.entries.get(id)
    if (!e) return
    const n = frameCount(e.el.duration, e.tm.fps)
    const k0 = targetFrame(tS, e.tm)
    if (!e.synced || (n !== null && (k0 < 0 || k0 >= n))) return // 首次对齐前、区间外停在首末帧时不计偏差
    const dev = frameOfVideo(mediaTimeS, e.tm.fps) - k0
    e.stat.presented++
    e.stat.lastDevFrames = dev
    e.stat.maxAbsDevFrames = Math.max(e.stat.maxAbsDevFrames, Math.abs(dev))
    const key = Math.max(-9, Math.min(9, dev))
    e.stat.hist[key] = (e.stat.hist[key] ?? 0) + 1
  }

  /** 某路视频的当前偏差（帧，按 currentTime；无元数据时为 null） */
  devFrames(id: string, tS: number): number | null {
    const e = this.entries.get(id)
    if (!e || e.el.readyState < 1) return null
    return frameOfVideo(e.el.currentTime, e.tm.fps) - targetFrame(tS, e.tm)
  }

  stats(): SyncStats {
    const videos = [...this.entries.values()].map((e) => ({ ...e.stat, hist: { ...e.stat.hist } }))
    const counted = videos.filter((v) => v.presented > 0)
    const hist: Record<number, number> = {}
    for (const v of videos) for (const [k, n] of Object.entries(v.hist)) hist[Number(k)] = (hist[Number(k)] ?? 0) + n
    return {
      videos,
      presented: videos.reduce((a, v) => a + v.presented, 0),
      maxAbsDevFrames: counted.length ? Math.max(...counted.map((v) => v.maxAbsDevFrames)) : null,
      hist,
      reseeks: videos.reduce((a, v) => a + v.reseeks, 0),
      buffering: this.buffering,
    }
  }

  /** 清零度量（拖动结束、切换视图后重新统计） */
  resetStats(): void {
    for (const e of this.entries.values()) {
      Object.assign(e.stat, { presented: 0, maxAbsDevFrames: 0, lastDevFrames: null, hist: {}, reseeks: 0, aligns: 0, nudges: 0 })
    }
  }
}

// ── 视角通道与设备档（AWR-14 §21.14.3） ────────────────────────────────────────────────────────────────────────────────

export type VideoKind = 'rgb' | 'thermal'
export type DeviceClass = 'desktop' | 'tablet' | 'phone'
export type ViewMode = 'rgb' | 'thermal' | 'both'
export const VIEW_MODES: readonly ViewMode[] = ['rgb', 'thermal', 'both']

/** 热成像通道的传感器名（S17：therm0；兼容 thermal0、ir0） */
const THERMAL_CAM_RE = /^(therm|thermal|ir)\d*$/i

/** 视频种类：索引给出 kind 时取之，否则按传感器名推断 */
export function videoKind(v: { kind?: VideoKind | null; cam: string }): VideoKind {
  return v.kind ?? (THERMAL_CAM_RE.test(v.cam) ? 'thermal' : 'rgb')
}

/** 设备档：宽 < 640 为手机，宽 < 1280 或粗指针为平板，其余为桌面（§21.14.3 表头） */
export function deviceClass(widthPx: number, coarse: boolean): DeviceClass {
  if (widthPx < 640) return 'phone'
  if (widthPx < 1280 || coarse) return 'tablet'
  return 'desktop'
}

/** 同时播放的视频路数上限：手机 1；平板与 Tier S（软件渲染）4（§21.7.4、§21.14.3）；桌面 Tier B/A 6（3 机 × 2 通道） */
export function maxConcurrentVideos(cls: DeviceClass, softwareRender: boolean): number {
  if (cls === 'phone') return 1
  if (cls === 'tablet' || softwareRender) return 4
  return 6
}

export interface UavChannels<V> { uav: string; rgb: V | null; thermal: V | null }

/** 按机号分组（机号升序）；同一机同一种类有多路时取传感器名最小的一路 */
export function groupByUav<V extends { uav: string; cam: string; kind?: VideoKind | null }>(videos: readonly V[]): UavChannels<V>[] {
  const m = new Map<string, UavChannels<V>>()
  for (const v of [...videos].sort((a, b) => a.cam.localeCompare(b.cam))) {
    const g = m.get(v.uav) ?? { uav: v.uav, rgb: null, thermal: null }
    const k = videoKind(v)
    if (!g[k]) g[k] = v
    m.set(v.uav, g)
  }
  return [...m.values()].sort((a, b) => a.uav.localeCompare(b.uav))
}

/** 可用的视图方式：某种类在任一机上存在才可选；并排需两种都有且总路数不超过上限 */
export function availableModes(groups: readonly UavChannels<unknown>[], limit: number): ViewMode[] {
  const nRgb = groups.filter((g) => g.rgb).length
  const nTh = groups.filter((g) => g.thermal).length
  const out: ViewMode[] = []
  if (nRgb) out.push('rgb')
  if (nTh) out.push('thermal')
  if (nRgb && nTh && nRgb + nTh <= limit) out.push('both')
  return out
}

/** 缺省视图方式：能并排就并排，否则 RGB，否则热成像 */
export function defaultMode(modes: readonly ViewMode[]): ViewMode | null {
  return modes.includes('both') ? 'both' : (modes[0] ?? null)
}

export interface Tile<V> { uav: string; kind: VideoKind; video: V }

/**
 * 当前显示的视频格：按机号排列；mode = both 时每机 RGB 在上一行、热成像在下一行（§21.29.3 线框）。手机只显示 focus 指定的
 * 一机（左右切换）；总路数截断到上限。
 */
export function visibleTiles<V>(groups: readonly UavChannels<V>[], mode: ViewMode, limit: number, focus: string | null = null): Tile<V>[] {
  const pick = limit === 1 ? groups.filter((g) => g.uav === (focus ?? groups[0]?.uav)) : groups
  const kinds: VideoKind[] = mode === 'both' ? ['rgb', 'thermal'] : [mode]
  const out: Tile<V>[] = []
  for (const k of kinds) {
    for (const g of pick) {
      const v = g[k]
      if (v) out.push({ uav: g.uav, kind: k, video: v })
    }
  }
  return out.slice(0, Math.max(1, limit))
}
