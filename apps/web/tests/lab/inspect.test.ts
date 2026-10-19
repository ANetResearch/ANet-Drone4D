// D3-AC-25（页面与同步部分）：/lab/runs/:rid/inspect 的 Rerun Web Viewer 嵌入（AWR-14 §21.9；AWR-05 §6.1.6、ADR-149、ADR-173）。
// - 播放头同步：lab 播放头 ↔ Viewer 双向，任一方向拖动后另一方误差 ≤ 1 帧（≤ 100 ms 仿真时间）；拖动中推送 ≤ 10 Hz；100 次往复后
//   无回环震荡（Viewer 收到的 set_current_time 次数 = 推送次数 = 被丢弃的回声数；播放头只在 Viewer 侧拖动时被回写，两侧事件计数一致），
//   静置后不再产生任何事件；回声延迟在 50 ms 窗口内外两种情形；Viewer 播放跟随与收回权威；
// - 选择联动：实体路径 ↔ 场景对象；真值实体只在评测 catalog 可定位；URL 片段保留已有参数；
// - 加载状态机：UNSUPPORTED、IDLE → LOADING（按字节进度）→ OPENING → READY；694、115、305、401 与 10 s 超时；链接含凭据拒绝；重试；
//   关闭后不再自动加载；卸载释放 Viewer；
// - 公开样例页的面板只读取静态切片：不调用 viewer_url、不发起任何 fetch，拒绝 catalog、bench-api 与外站地址及超过 64 MB 的切片；
// - 按需加载与 D1-AC-03b 不回退：src 中只有 viewerLoader.ts 引用 @rerun-io/web-viewer，且只以动态 import()；面板的静态导入图不含
//   net/bench、应用路由与应用词典，可进入 /lab/ 公开只读构建；
// - 配置：memory_limit ≤ 1.5 GB、hide_welcome_screen、不检查更新、隐藏顶栏与选择面板；版本锁定 0.38.1（npm、rerun-sdk、BOM）；
//   Viewer 许可登记（BOM T84 与 THIRD_PARTY_NOTICES）；
// - 路由分片、文案分片与外壳文案（AWR-14 §21.9.2 原文）。
// 浏览器中的端到端（真实 Wasm、Cookie 鉴权、首帧 ≤ 5 s 实测）由 R07 的 Playwright `lab/inspect`、`lab/public-samples` 交付。
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LAB_PUBLIC_FORBIDDEN_RE } from '../../vite.config.ts'
import { setLocale, t } from '@/app/i18n'
import { formatMessage } from '@/app/i18n/plural'
import { usePlayhead } from '@/stores/lab/playhead'
import {
  checkLink, checkStaticSource, decideSupport, errorOf, hasCredential, inspectSupport, isStaticRrd, nativeCommand, parseInspectQuery, SLICE_MAX_BYTES,
  SOFTWARE_RENDERER_RE, viewerUrlPath, type CatalogSource, type DeviceProbe, type StaticSource, type ViewerLink,
} from '@/ui/lab/inspect/inspect'
import { InspectController } from '@/ui/lab/inspect/inspectController'
import { InspectPage } from '@/ui/lab/inspect/InspectPage'
import { InspectPanel } from '@/ui/lab/inspect/InspectPanel'
import { labPlayheadPort } from '@/ui/lab/inspect/playheadPort'
import { inspectText } from '@/ui/lab/inspect/text'
import {
  ECHO_KEEP_MS, entityToSelection, FRAME_NS, RerunSync, selectionEntity, selectionFromItems, toNs, withSelectionFragment, type PlayheadPort,
  type PlayheadSnap, type SyncClock, type ViewerEventMap,
} from '@/ui/lab/inspect/rerunSync'
import {
  HIDDEN_PANELS, parseMemoryLimit, RERUN_VIEWER_VERSION, startViewer, VIEWER_MEMORY_LIMIT, VIEWER_MEMORY_LIMIT_MAX, VIEWER_WASM_BYTES,
  viewerStartOptions, withWasmProgress, type ViewerHandle, type ViewerModule,
} from '@/ui/lab/inspect/viewerLoader'
import { SOFTWARE_RE } from '@/viewport/backend/deviceClass'

const WEB = resolve(import.meta.dirname, '..', '..')
const ROOT = resolve(WEB, '..', '..')
const SRC = join(WEB, 'src')

// ── 假时钟与假 Viewer ─────────────────────────────────────────────────────────────────────────────────────────────────

class FakeClock implements SyncClock {
  t = 0
  private seq = 0
  private q = new Map<number, { at: number; fn: () => void }>()
  now = (): number => this.t
  setTimeout = (fn: () => void, ms: number): unknown => {
    const id = ++this.seq
    this.q.set(id, { at: this.t + Math.max(0, ms), fn })
    return id
  }
  clearTimeout = (h: unknown): void => {
    this.q.delete(h as number)
  }
  /** 推进 ms，按到期时刻依次执行（执行中新排的定时器同样参与） */
  advance(ms: number): void {
    const end = this.t + ms
    for (;;) {
      let best: [number, { at: number; fn: () => void }] | null = null
      for (const e of this.q) if (e[1].at <= end && (!best || e[1].at < best[1].at || (e[1].at === best[1].at && e[0] < best[0]))) best = e
      if (!best) break
      this.q.delete(best[0])
      this.t = Math.max(this.t, best[1].at)
      best[1].fn()
    }
    this.t = end
  }
  get pending(): number {
    return this.q.size
  }
}

type Listener = (e: never) => void

/** 行为与 0.38 WebViewer 一致的假 Viewer：set_current_time 后在下一个 tick（或给定延迟）派发 time_update 回声；事件异步派发 */
class FakeViewer implements ViewerHandle {
  time = 0
  playing = false
  timeline = 'sim_time'
  setTimeCalls = 0
  emitted: Record<string, number> = {}
  opened: (string | string[])[] = []
  closed: (string | string[])[] = []
  stopped = 0
  panels: Record<string, string | null> = {}
  range: { min: number; max: number } | null = { min: 0, max: 60e9 }
  private ls = new Map<string, Set<Listener>>()
  constructor(private readonly clock: FakeClock, private readonly echoDelay: () => number = () => 0, readonly rec = 'run-k2m4q7xz3a') {}
  on<K extends keyof ViewerEventMap>(ev: K, cb: (e: ViewerEventMap[K]) => void): () => void {
    const s = this.ls.get(ev) ?? new Set()
    s.add(cb as Listener)
    this.ls.set(ev, s)
    return () => s.delete(cb as Listener)
  }
  /** 立即派发（测试中模拟用户在 Viewer 中的操作）；Viewer 实际在下一个 tick 派发，见 echo */
  emit<K extends keyof ViewerEventMap>(ev: K, e: ViewerEventMap[K]): void {
    this.emitted[ev] = (this.emitted[ev] ?? 0) + 1
    for (const cb of [...(this.ls.get(ev) ?? [])]) (cb as (x: ViewerEventMap[K]) => void)(e)
  }
  set_current_time(rec: string, tl: string, ns: number): void {
    expect(rec).toBe(this.rec)
    expect(tl).toBe('sim_time')
    this.setTimeCalls++
    this.time = ns
    this.clock.setTimeout(() => this.emit('time_update', { recording_id: this.rec, time: ns }), this.echoDelay())
  }
  /** 用户在 Viewer 的时间面板拖动到 ns */
  userScrub(ns: number): void {
    this.time = ns
    this.emit('time_update', { recording_id: this.rec, time: ns })
  }
  set_playing(_r: string, v: boolean): void {
    this.playing = v
  }
  set_active_timeline(_r: string, tl: string): void {
    this.timeline = tl
  }
  open(rrd: string | string[]): void {
    this.opened.push(rrd)
  }
  close(rrd: string | string[]): void {
    this.closed.push(rrd)
  }
  stop(): void {
    this.stopped++
  }
  override_panel_state(p: string, s: string | null): void {
    this.panels[p] = s
  }
  get_time_range(): { min: number; max: number } | null {
    return this.range
  }
  get_current_time(): number {
    return this.time
  }
  get listenerCount(): number {
    let n = 0
    for (const s of this.ls.values()) n += s.size
    return n
  }
}

/** 内存播放头（与 stores/lab/playhead.ts 同一语义：seek 夹在 [0, duration]，订阅同步回调） */
class MemPlayhead implements PlayheadPort {
  s: PlayheadSnap = { tS: 0, playing: false }
  duration = 600
  seeksFromSync = 0
  private ls = new Set<(s: PlayheadSnap, p: PlayheadSnap) => void>()
  get(): PlayheadSnap {
    return this.s
  }
  private write(n: PlayheadSnap): void {
    const p = this.s
    this.s = n
    for (const l of [...this.ls]) l(n, p)
  }
  seek(tS: number): void {
    this.seeksFromSync++
    this.duration = Math.max(this.duration, tS)
    this.write({ ...this.s, tS: Math.min(Math.max(0, tS), this.duration) })
  }
  /** 用户在时间线或场景中拖动（不经同步器） */
  user(tS: number): void {
    this.write({ ...this.s, tS: Math.min(Math.max(0, tS), this.duration) })
  }
  play(): void {
    this.write({ ...this.s, playing: true })
  }
  pause(): void {
    this.write({ ...this.s, playing: false })
  }
  subscribe(fn: (s: PlayheadSnap, p: PlayheadSnap) => void): () => void {
    this.ls.add(fn)
    return () => this.ls.delete(fn)
  }
}

function rng(seed: number): () => number {
  let x = seed >>> 0
  return () => {
    x = (x * 1664525 + 1013904223) >>> 0
    return x / 2 ** 32
  }
}

const REC = 'run-k2m4q7xz3a'

function setup(echoDelay: () => number = () => 0) {
  const clock = new FakeClock()
  const viewer = new FakeViewer(clock, echoDelay, REC)
  const ph = new MemPlayhead()
  const sync = new RerunSync(viewer, ph, { clock })
  sync.attach(REC)
  clock.advance(50)
  return { clock, viewer, ph, sync }
}

const errNs = (ph: MemPlayhead, viewer: FakeViewer) => Math.abs(toNs(ph.get().tS) - viewer.time)

// ── 播放头同步 ────────────────────────────────────────────────────────────────────────────────────────────────────────

describe('lab playhead <-> Rerun Viewer sync (AWR-14 §21.9.3; D3-AC-25)', () => {
  it('aligns the viewer to the playhead on attach and sets the sim_time timeline', () => {
    const clock = new FakeClock()
    const viewer = new FakeViewer(clock)
    viewer.timeline = 'tick'
    const ph = new MemPlayhead()
    ph.user(37.25)
    const sync = new RerunSync(viewer, ph, { clock })
    sync.attach(REC)
    expect(viewer.timeline).toBe('sim_time')
    expect(viewer.time).toBe(37_250_000_000)
    clock.advance(10)
    expect(sync.stats()).toMatchObject({ pushed: 1, echoes: 1, applied: 0 })
  })

  it('pauses the viewer on attach and drops events queued before it (0.38 auto-plays an opened .rrd without a play event)', () => {
    const clock = new FakeClock()
    const viewer = new FakeViewer(clock, () => 5)
    viewer.playing = true
    const ph = new MemPlayhead()
    ph.user(37.25)
    // 打开时已排队（实测顺序）：timeline_change(tick)、time_update(0)、time_update(3)，均在 attach 之后才派发
    clock.setTimeout(() => viewer.emit('timeline_change', { recording_id: REC, timeline: 'tick', time: 0 }), 0)
    clock.setTimeout(() => viewer.emit('time_update', { recording_id: REC, time: 0 }), 0)
    clock.setTimeout(() => viewer.emit('time_update', { recording_id: REC, time: 3 }), 1)
    const sync = new RerunSync(viewer, ph, { clock })
    sync.attach(REC)
    expect(viewer.playing).toBe(false)
    clock.advance(100)
    expect(ph.get().tS).toBe(37.25)
    expect(viewer.time).toBe(37_250_000_000)
    expect(sync.stats()).toMatchObject({ pushed: 1, echoes: 1, applied: 0, stale: 2 })
    viewer.userScrub(12e9)                                            // 回声之后的事件正常回写（时间轴仍为 sim_time）
    expect(ph.get().tS).toBe(12)
  })

  it('ends the settle window after ECHO_KEEP_MS when the viewer never echoes', () => {
    const clock = new FakeClock()
    const viewer = new FakeViewer(clock, () => 5)
    viewer.set_current_time = (_r, _tl, ns) => {
      viewer.setTimeCalls++
      viewer.time = ns
    }
    const ph = new MemPlayhead()
    const sync = new RerunSync(viewer, ph, { clock })
    sync.attach(REC)
    viewer.userScrub(5e9)
    expect(ph.get().tS).toBe(0)
    clock.advance(ECHO_KEEP_MS)
    viewer.userScrub(6e9)
    expect(ph.get().tS).toBe(6)
    expect(sync.stats().stale).toBe(1)
  })

  it('throttles pushes while scrubbing to <= 10 Hz and pushes the exact time when the drag ends', () => {
    const { clock, viewer, ph, sync } = setup()
    const before = viewer.setTimeCalls
    sync.setScrubbing(true)
    // 1 s 的拖动，每 16 ms 一次（约 60 次），每次移动 0.37 s
    const t0 = clock.now()
    let pushTimes: number[] = []
    const orig = viewer.set_current_time.bind(viewer)
    viewer.set_current_time = (r, tl, ns) => {
      pushTimes.push(clock.now())
      orig(r, tl, ns)
    }
    for (let i = 1; i <= 62; i++) {
      ph.user(i * 0.37)
      clock.advance(16)
    }
    const during = viewer.setTimeCalls - before
    expect(during).toBeLessThanOrEqual(Math.ceil(((clock.now() - t0) / 1000) * 10) + 1)
    for (let i = 1; i < pushTimes.length; i++) expect(pushTimes[i]! - pushTimes[i - 1]!).toBeGreaterThanOrEqual(100 - 1e-9)
    // 拖动中误差有界（节流窗口内的移动量）；拖动结束后误差为 0
    sync.setScrubbing(false)
    expect(errNs(ph, viewer)).toBe(0)
    pushTimes = []
    clock.advance(1000)
    expect(sync.stats().applied).toBe(0)
  })

  it('does not push sub-frame changes when not scrubbing, so the viewer stays within one frame', () => {
    const { clock, viewer, ph, sync } = setup()
    const n0 = viewer.setTimeCalls
    // 本地时钟以 60 Hz 推进 3 s（rate 1）：每 100 ms 仿真时间推送一次
    for (let i = 1; i <= 180; i++) {
      ph.user(i / 60)
      expect(errNs(ph, viewer)).toBeLessThan(FRAME_NS)
      clock.advance(1000 / 60)
    }
    expect(viewer.setTimeCalls - n0).toBeGreaterThanOrEqual(28)
    expect(viewer.setTimeCalls - n0).toBeLessThanOrEqual(31)
    expect(sync.stats().applied).toBe(0)
  })

  for (const [name, delay] of [
    ['echo on the next tick', () => 0],
    ['echo within the 50 ms window (0-40 ms)', (() => { const r = rng(7); return () => r() * 40 })()],
    ['echo after the window (60-400 ms)', (() => { const r = rng(11); return () => 60 + r() * 340 })()],
  ] as const) {
    it(`100 round trips: error <= 1 frame both ways, no loop, equal event counts (${name})`, () => {
      const { clock, viewer, ph, sync } = setup(delay)
      const r = rng(2026)
      let userViewerEvents = 0
      for (let trip = 0; trip < 100; trip++) {
        // (a) 在时间线 / 场景视图中拖动：Viewer 只收到推送，播放头不被回写
        const s0 = sync.stats()
        const calls0 = viewer.setTimeCalls
        sync.setScrubbing(true)
        const target = r() * 590
        const from = ph.get().tS
        for (let k = 1; k <= 12; k++) {
          ph.user(from + ((target - from) * k) / 12)
          clock.advance(25)
        }
        sync.setScrubbing(false)
        expect(errNs(ph, viewer)).toBeLessThanOrEqual(FRAME_NS)
        clock.advance(500)                                            // 回声全部到达
        expect(errNs(ph, viewer)).toBeLessThanOrEqual(FRAME_NS)
        const s1 = sync.stats()
        expect(s1.applied).toBe(s0.applied)                           // 回声从不回写播放头
        expect(viewer.setTimeCalls - calls0).toBe(s1.pushed - s0.pushed)
        // (b) 在 Viewer 中拖动：播放头被回写，Viewer 不再收到推送
        const calls1 = viewer.setTimeCalls
        const vt = Math.round(r() * 590e9)
        const v0 = viewer.time
        for (let k = 1; k <= 8; k++) {
          viewer.userScrub(Math.round(v0 + ((vt - v0) * k) / 8))
          userViewerEvents++
          clock.advance(16)
        }
        clock.advance(500)
        expect(errNs(ph, viewer)).toBeLessThanOrEqual(FRAME_NS)
        expect(viewer.setTimeCalls).toBe(calls1)                      // 回写不触发推送（防重入）
      }
      // 静置 60 s：两侧都不再产生事件（无回环震荡）
      const end = { calls: viewer.setTimeCalls, ev: viewer.emitted.time_update ?? 0, st: sync.stats() }
      clock.advance(60_000)
      expect(viewer.setTimeCalls).toBe(end.calls)
      expect(viewer.emitted.time_update ?? 0).toBe(end.ev)
      expect(sync.stats()).toEqual(end.st)
      expect(clock.pending).toBe(0)
      // 计数一致：lab → Viewer 的推送 = Viewer 收到的 set_current_time = 被识别的回声；Viewer → lab 的用户事件全部被回写或合并
      const st = sync.stats()
      expect(st.pushed).toBe(viewer.setTimeCalls)
      expect(st.echoes).toBe(st.pushed)
      expect(viewer.emitted.time_update).toBe(st.pushed + userViewerEvents)
      expect(st.applied).toBe(ph.seeksFromSync)
      expect(st.applied).toBeGreaterThanOrEqual(100)
      expect(st.applied).toBeLessThanOrEqual(userViewerEvents)
      expect(st.reclaimed).toBe(0)
    })
  }

  it('coalesces sub-frame viewer updates and writes the exact time 100 ms later', () => {
    const { clock, viewer, ph, sync } = setup()
    viewer.userScrub(10e9)
    expect(ph.get().tS).toBe(10)
    viewer.userScrub(10.05e9)                                        // < 1 帧：合并
    expect(ph.get().tS).toBe(10)
    clock.advance(100)
    expect(ph.get().tS).toBe(10.05)
    expect(sync.stats().coalesced).toBeGreaterThanOrEqual(1)
  })

  it('follows the viewer while it plays and hands authority back on pause or on a user action', () => {
    const { clock, viewer, ph, sync } = setup()
    const sources: string[] = []
    sync.onSource((s) => sources.push(s))
    ph.play()
    viewer.emit('play', { recording_id: REC })
    expect(sync.source).toBe('rerun')
    expect(ph.get().playing).toBe(false)                              // 本地时钟暂停，Viewer 驱动
    for (let i = 1; i <= 60; i++) {
      viewer.userScrub(Math.round((i / 60) * 1e9))
      clock.advance(1000 / 60)
      expect(errNs(ph, viewer)).toBeLessThanOrEqual(FRAME_NS)
    }
    viewer.emit('pause', { recording_id: REC })
    expect(errNs(ph, viewer)).toBe(0)
    expect(sync.source).toBe('lab')
    // 再次播放，用户在时间线上操作即收回权威并暂停 Viewer
    viewer.emit('play', { recording_id: REC })
    viewer.playing = true
    ph.user(30)
    expect(sync.source).toBe('lab')
    expect(viewer.playing).toBe(false)
    expect(viewer.time).toBe(30e9)
    expect(sync.stats().reclaimed).toBe(1)
    expect(sources).toEqual(['rerun', 'lab', 'rerun', 'lab'])
  })

  it('ignores other timelines and other recordings', () => {
    const { viewer, ph } = setup()
    viewer.emit('timeline_change', { recording_id: REC, timeline: 'tick', time: 5 })
    viewer.userScrub(9000)
    expect(ph.get().tS).toBe(0)
    viewer.emit('timeline_change', { recording_id: REC, timeline: 'sim_time', time: 0 })
    viewer.emit('time_update', { recording_id: 'run-other00000', time: 9e9 })
    expect(ph.get().tS).toBe(0)
    viewer.userScrub(9e9)
    expect(ph.get().tS).toBe(9)
  })

  it('detaches all listeners and timers', () => {
    const { clock, viewer, sync } = setup()
    expect(viewer.listenerCount).toBe(4)                               // time_update、timeline_change、play、pause
    sync.setScrubbing(true)
    sync.detach()
    expect(viewer.listenerCount).toBe(0)
    clock.advance(1000)
    expect(clock.pending).toBe(0)
  })

  it('adapts the lab playhead store and extends its duration for viewer times beyond it', () => {
    usePlayhead.setState({ tS: 0, playing: false, durationS: 5 })
    const seen: number[] = []
    const off = labPlayheadPort.subscribe((s) => seen.push(s.tS))
    labPlayheadPort.seek(42.5)
    expect(usePlayhead.getState().tS).toBe(42.5)
    expect(usePlayhead.getState().durationS).toBe(42.5)
    expect(seen.at(-1)).toBe(42.5)
    off()
  })
})

// ── 选择联动 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

describe('selection linkage (AWR-14 §21.9.3 item 3)', () => {
  it('maps viewer entity paths to scene objects', () => {
    expect(entityToSelection('/fleet/uav-02')).toEqual({ kind: 'uav', id: 'uav-02' })
    expect(entityToSelection('/fleet/uav-02/cam0/image/rgb')).toEqual({ kind: 'uav', id: 'uav-02' })
    expect(entityToSelection('/truth/fleet/uav-03')).toEqual({ kind: 'uav', id: 'uav-03' })
    expect(entityToSelection('/truth/actors/ped-007/shape')).toEqual({ kind: 'actor', id: 'ped-007' })
    expect(entityToSelection('/world/basemap')).toBeNull()
    expect(entityToSelection('/fleetx/uav-01')).toBeNull()
    expect(selectionFromItems([{ type: 'view' }, { type: 'entity', entity_path: '/world' }, { type: 'entity', entity_path: '/fleet/uav-01/lidar0' }]))
      .toEqual({ kind: 'uav', id: 'uav-01' })
  })

  it('locates scene selections in the viewer; truth entities only with truth access', () => {
    expect(selectionEntity({ kind: 'uav', id: 'uav-02' }, false)).toBe('/fleet/uav-02')
    expect(selectionEntity({ kind: 'actor', id: 'ped-007' }, false)).toBeNull()
    expect(selectionEntity({ kind: 'actor', id: 'ped-007' }, true)).toBe('/truth/actors/ped-007')
    expect(selectionEntity({ kind: 'uav', id: '../x' }, true)).toBeNull()
    const u = 'rerun+https://lab.example/rerun/catalog/dist/dataset/w.e?segment_id=run-k2m4q7xz3a#when=sim_time@85s&selection=/fleet/uav-01'
    expect(withSelectionFragment(u, '/fleet/uav-02'))
      .toBe('rerun+https://lab.example/rerun/catalog/dist/dataset/w.e?segment_id=run-k2m4q7xz3a#when=sim_time@85s&selection=/fleet/uav-02')
    expect(withSelectionFragment('rerun+https://h/x', '/fleet/uav-01')).toBe('rerun+https://h/x#selection=/fleet/uav-01')
  })
})

// ── 加载状态机 ────────────────────────────────────────────────────────────────────────────────────────────────────────

const LINK: ViewerLink = {
  url: 'rerun+https://lab.example/rerun/catalog/dist/dataset/oryahovo-crop1.abc-crossing-001-v3?segment_id=run-k2m4q7xz3a',
  recording_id: REC, timeline: 'sim_time', time_range_ns: [85e9, 95e9], catalog: 'dist', rerun_version: '0.38.1',
  cookie_exp_unix_ns: '1793000600000000000', migrate_hint: null,
}

function ctlSetup(o: { source?: CatalogSource | StaticSource; support?: { ok: true } | { ok: false; reason: 'software' }; fail?: Error } = {}) {
  const clock = new FakeClock()
  const viewer = new FakeViewer(clock)
  const ph = new MemPlayhead()
  const fetchLink = vi.fn(async () => LINK)
  const source = o.source ?? { kind: 'catalog' as const, catalog: 'dist' as const, fetchLink }
  const starts: number[] = []
  const selected: unknown[] = []
  const ranges: [number, number][] = []
  const ctl = new InspectController({
    source, playhead: ph, support: o.support ?? { ok: true }, clock,
    resolveUrl: (u) => new URL(u, 'https://drone4d.example/lab/samples/run-k2m4q7xz3a').href,
    start: async (_p, so) => {
      starts.push(1)
      if (o.fail) throw o.fail
      so.onBytes?.(VIEWER_WASM_BYTES / 2)
      return viewer
    },
    onSelect: (s) => selected.push(s),
    onRange: (a, b) => ranges.push([a, b]),
  })
  const parent = {} as HTMLElement
  return { clock, viewer, ph, ctl, parent, fetchLink, starts, selected, ranges }
}
const flush = () => new Promise((r) => setTimeout(r, 0))

describe('inspect panel state machine (AWR-14 §21.9.2)', () => {
  it('UNSUPPORTED on Tier S and similar devices; never loads', async () => {
    const { ctl, parent, starts } = ctlSetup({ support: { ok: false, reason: 'software' } })
    expect(ctl.snapshot.state).toBe('UNSUPPORTED')
    await ctl.start(parent)
    expect(starts).toHaveLength(0)
    expect(ctl.snapshot.state).toBe('UNSUPPORTED')
  })

  it('IDLE -> LOADING -> OPENING -> READY through viewer_url and recording_open, then syncs', async () => {
    const { ctl, parent, viewer, ph, fetchLink, ranges, clock } = ctlSetup()
    const states: string[] = []
    ctl.subscribe(() => states.push(ctl.snapshot.state))
    expect(ctl.snapshot.state).toBe('IDLE')
    const p = ctl.start(parent)
    expect(ctl.snapshot.state).toBe('LOADING')
    await p
    expect(fetchLink).toHaveBeenCalledTimes(1)
    expect(ctl.snapshot.state).toBe('OPENING')
    expect(ctl.snapshot.receivedBytes).toBe(VIEWER_WASM_BYTES)
    expect(viewer.opened).toEqual([LINK.url])
    viewer.emit('recording_open', { recording_id: 'run-other00000', source: 'http' })
    expect(ctl.snapshot.state).toBe('OPENING')
    viewer.emit('recording_open', { recording_id: REC, source: 'redap', version: '0.38.1' })
    expect(ctl.snapshot.state).toBe('READY')
    expect(states).toEqual(expect.arrayContaining(['LOADING', 'OPENING', 'READY']))
    expect(ranges).toEqual([[0, 60]])
    clock.advance(10)                                                 // attach 推送的回声
    viewer.userScrub(12e9)
    expect(ph.get().tS).toBe(12)
    ph.user(20)
    expect(viewer.time).toBe(20e9)
    clock.advance(OPEN_TIMEOUT)
    expect(ctl.snapshot.state).toBe('READY')
  })

  it('reports 694, 115, 305, 401 and other failures of viewer_url as panel errors', async () => {
    for (const [err, kind] of [
      [{ status: 409, code: 694, message: 'RERUN_VERSION_MISMATCH' }, 'version'],
      [{ status: 403, code: 115 }, 'role'],
      [{ status: 404, code: 305 }, 'notFound'],
      [{ status: 401, code: null }, 'session'],
      [new TypeError('fetch failed'), 'unknown'],
    ] as const) {
      const fetchLink = vi.fn(async () => {
        throw err
      })
      const { ctl, parent, viewer } = ctlSetup({ source: { kind: 'catalog', catalog: 'dist', fetchLink } })
      await ctl.start(parent)
      expect(ctl.snapshot.state).toBe('ERROR')
      expect(ctl.snapshot.error?.kind).toBe(kind)
      expect(viewer.opened).toEqual([])
    }
  })

  it('refuses links with credentials and recordings of another minor version', async () => {
    for (const [link, kind] of [
      [{ ...LINK, url: `${LINK.url}&token=d3st_abc` }, 'credential'],
      [{ ...LINK, url: 'rerun+https://user:pw@lab.example/rerun/catalog/dist' }, 'credential'],
      [{ ...LINK, rerun_version: '0.37.2', migrate_hint: 'rerun rrd migrate' }, 'version'],
    ] as const) {
      const { ctl, parent, viewer } = ctlSetup({ source: { kind: 'catalog', catalog: 'dist', fetchLink: async () => link } })
      await ctl.start(parent)
      expect(ctl.snapshot.error?.kind).toBe(kind)
      expect(viewer.opened).toEqual([])
    }
    const { ctl, parent, viewer } = ctlSetup()
    await ctl.start(parent)
    viewer.emit('recording_open', { recording_id: REC, version: '0.39.0' })
    expect(ctl.snapshot.state).toBe('ERROR')
    expect(ctl.snapshot.error?.kind).toBe('version')
  })

  it('times out after 10 s without recording_open; retry closes and reopens without reloading', async () => {
    const { ctl, parent, viewer, clock, starts, fetchLink } = ctlSetup()
    await ctl.start(parent)
    clock.advance(OPEN_TIMEOUT - 1)
    expect(ctl.snapshot.state).toBe('OPENING')
    clock.advance(1)
    expect(ctl.snapshot.state).toBe('ERROR')
    expect(ctl.snapshot.error?.kind).toBe('timeout')
    await ctl.retry()
    expect(starts).toHaveLength(1)
    expect(fetchLink).toHaveBeenCalledTimes(2)
    expect(viewer.closed).toEqual([LINK.url])
    viewer.emit('recording_open', { recording_id: REC })
    expect(ctl.snapshot.state).toBe('READY')
  })

  it('a failed load is an error; retry loads again', async () => {
    const { ctl, parent, starts } = ctlSetup({ fail: new Error('Your browser is too old to run the Rerun Viewer') })
    await ctl.start(parent)
    expect(ctl.snapshot.error?.kind).toBe('load')
    await ctl.retry()
    expect(starts).toHaveLength(2)
  })

  it('close stops the viewer and disables auto start; dispose releases the viewer and keeps auto start', async () => {
    const a = ctlSetup()
    await a.ctl.start(a.parent)
    a.viewer.emit('recording_open', { recording_id: REC })
    a.ctl.close()
    expect(a.viewer.stopped).toBe(1)
    expect(a.ctl.snapshot).toMatchObject({ state: 'IDLE', autoStart: false })
    expect(a.viewer.listenerCount).toBe(0)
    const b = ctlSetup()
    const p = b.ctl.start(b.parent)
    b.ctl.dispose()                                                   // 加载在途时卸载：加载完成后立即停止，不再改变状态
    await p
    await flush()
    expect(b.viewer.stopped).toBe(1)
    expect(b.ctl.snapshot).toMatchObject({ state: 'IDLE', autoStart: true })
  })

  it('locates scene selections through the URL fragment and forwards viewer selections', async () => {
    const { ctl, parent, viewer, selected } = ctlSetup()
    await ctl.start(parent)
    expect(ctl.selectInViewer({ kind: 'uav', id: 'uav-02' }, false)).toBe(false)   // READY 之前不定位
    viewer.emit('recording_open', { recording_id: REC })
    expect(ctl.selectInViewer({ kind: 'uav', id: 'uav-02' }, false)).toBe(true)
    expect(viewer.opened.at(-1)).toBe(`${LINK.url}#selection=/fleet/uav-02`)
    expect(ctl.selectInViewer({ kind: 'actor', id: 'ped-007' }, false)).toBe(false)
    viewer.emit('selection_change', { recording_id: REC, items: [{ type: 'entity', entity_path: '/truth/actors/ped-007' }] })
    expect(selected).toEqual([{ kind: 'actor', id: 'ped-007' }])
  })
})

const OPEN_TIMEOUT = 10_000

// ── 公开样例页：只读静态切片 ──────────────────────────────────────────────────────────────────────────────────────────

const STATIC: StaticSource = {
  kind: 'static', recordingId: REC,
  slices: [{ url: '/lab-static/v/2026.10.19-1/runs/run-k2m4q7xz3a/slices/t085-095.rrd', bytes: 8_177_000 }],
  basemap: { url: '/lab-static/v/2026.10.19-1/runs/run-k2m4q7xz3a/slices/basemap.rrd', bytes: 5_955_000 },
}

describe('public sample panel reads static slices only (AWR-14 §21.9.4; D3-AC-25)', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('opens slice and basemap together and makes no request of its own', async () => {
    const fetchSpy = vi.fn(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetchSpy)
    const { ctl, parent, viewer } = ctlSetup({ source: STATIC })
    await ctl.start(parent)
    expect(viewer.opened).toEqual([[
      'https://drone4d.example/lab-static/v/2026.10.19-1/runs/run-k2m4q7xz3a/slices/t085-095.rrd',
      'https://drone4d.example/lab-static/v/2026.10.19-1/runs/run-k2m4q7xz3a/slices/basemap.rrd',
    ]])
    viewer.emit('recording_open', { recording_id: REC })
    expect(ctl.snapshot.state).toBe('READY')
    expect(ctl.snapshot.link).toBeNull()
    expect(fetchSpy).not.toHaveBeenCalled()
    for (const u of viewer.opened.flat()) expect(u).not.toMatch(/\/(api|rerun)\/|catalog|^rerun\+/)
    expect(ctl.selectInViewer({ kind: 'uav', id: 'uav-01' }, false)).toBe(false)    // 静态切片不重新打开
  })

  it('rejects catalog, bench-api, rerun-proxy and foreign addresses and oversize slices', async () => {
    expect(isStaticRrd('/lab-static/v/1/runs/r/slices/a.rrd')).toBe(true)
    expect(isStaticRrd('v/1/runs/r/slices/a.rrd')).toBe(true)
    expect(isStaticRrd('/lab/samples/r/slice.rrd')).toBe(true)
    for (const bad of [
      'rerun+https://lab.example/rerun/catalog/dist/dataset/x', 'https://cdn.example/a.rrd', '//cdn.example/a.rrd', '/api/bench/v1/runs/r/a.rrd',
      '/rerun/catalog/dist/a.rrd', '/lab-static/../api/a.rrd', '/runs/r/full.rrd', '/lab-static/v/1/index.json', 'rerun/a.rrd',
    ]) expect(isStaticRrd(bad), bad).toBe(false)
    expect(checkStaticSource(STATIC)).toBeNull()
    expect(checkStaticSource({ ...STATIC, slices: [{ url: STATIC.slices[0]!.url, bytes: SLICE_MAX_BYTES + 1 }] })?.kind).toBe('static')
    expect(checkStaticSource({ ...STATIC, basemap: { url: STATIC.basemap!.url, bytes: 25 * 2 ** 20 } })?.kind).toBe('static')
    expect(checkStaticSource({ ...STATIC, slices: [] })?.kind).toBe('static')
    const { ctl, parent, viewer } = ctlSetup({ source: { ...STATIC, slices: [{ url: 'rerun+http://127.0.0.1:9876/proxy' }] } })
    await ctl.start(parent)
    expect(ctl.snapshot.error?.kind).toBe('static')
    expect(viewer.opened).toEqual([])
  })
})

// ── 纯函数 ────────────────────────────────────────────────────────────────────────────────────────────────────────────

describe('inspect helpers', () => {
  it('parses t, slice and catalog and builds the BN-57 request', () => {
    expect(parseInspectQuery(new URLSearchParams('t=37.25&slice=85,95&catalog=eval'))).toEqual({ t: 37.25, slice: [85, 95], catalog: 'eval' })
    expect(parseInspectQuery(new URLSearchParams('t=-1&slice=95,85&catalog=x'))).toEqual({ t: null, slice: null, catalog: 'dist' })
    expect(viewerUrlPath('run-k2m4q7xz3a', { slice: [85, 95], catalog: 'dist' })).toBe('/runs/run-k2m4q7xz3a/viewer_url?t_from=85&t_to=95&catalog=dist')
    expect(viewerUrlPath('run-k2m4q7xz3a', { slice: null, catalog: 'eval' })).toBe('/runs/run-k2m4q7xz3a/viewer_url?catalog=eval')
  })

  it('validates viewer links and maps reason codes', () => {
    expect(checkLink(LINK, { catalog: 'dist' })).toBeNull()
    expect(checkLink(LINK, { catalog: 'eval' })?.kind).toBe('unknown')
    expect(hasCredential(LINK.url)).toBe(false)
    for (const u of ['x?access_token=1', 'x#d3_rr=abc', 'x/d3rr_abcdef', 'https://a:b@h/']) expect(hasCredential(u), u).toBe(true)
    expect(errorOf({ status: 409, code: 694 }).kind).toBe('version')
    expect(errorOf(null).kind).toBe('unknown')
  })

  it('enables the panel only on hardware GPUs in desktop layouts (Tier B/A)', () => {
    const ok: DeviceProbe = { webgl2: true, webgpu: false, renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070)', simd: true, coarse: false, widthPx: 1920 }
    expect(decideSupport(ok)).toEqual({ ok: true })
    expect(decideSupport({ ...ok, renderer: 'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)))' })).toEqual({ ok: false, reason: 'software' })
    expect(decideSupport({ ...ok, webgl2: false })).toEqual({ ok: false, reason: 'nogpu' })
    expect(decideSupport({ ...ok, webgl2: false, webgpu: true, renderer: '' })).toEqual({ ok: true })
    expect(decideSupport({ ...ok, simd: false })).toEqual({ ok: false, reason: 'simd' })
    expect(decideSupport({ ...ok, coarse: true })).toEqual({ ok: false, reason: 'layout' })
    expect(decideSupport({ ...ok, widthPx: 800 })).toEqual({ ok: false, reason: 'layout' })
    expect(SOFTWARE_RENDERER_RE.source).toBe(SOFTWARE_RE.source)
    expect(SOFTWARE_RENDERER_RE.flags).toBe(SOFTWARE_RE.flags)
  })

  it('lets test builds force the panel on software rendering with ?inspect=force (Playwright, R07)', () => {
    const sw = (): DeviceProbe => ({ webgl2: true, webgpu: false, renderer: 'SwiftShader', simd: true, coarse: false, widthPx: 1920 })
    expect(inspectSupport(new URLSearchParams(''), sw)).toEqual({ ok: false, reason: 'software' })
    expect(inspectSupport(new URLSearchParams('inspect=force'), sw)).toEqual({ ok: true })        // vitest：TEST_SWITCHES = DEV
  })

  it('native viewer command carries no token', () => {
    expect(nativeCommand('run-k2m4q7xz3a', 37.24)).toBe('python -m awr.bench.rerun.open --run run-k2m4q7xz3a --t 37.2')
    expect(nativeCommand('run-k2m4q7xz3a', 0, 'eval')).toBe('python -m awr.bench.rerun.open --run run-k2m4q7xz3a --catalog eval')
    expect(hasCredential(nativeCommand('run-k2m4q7xz3a', 1))).toBe(false)
  })
})

// ── 加载、配置、版本与许可 ────────────────────────────────────────────────────────────────────────────────────────────

describe('viewer loading and configuration (AWR-05 §6.1.5, §6.1.6)', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('starts with memory_limit <= 1.5 GB, no welcome screen, no update check, host theme; hides top and selection panels', async () => {
    const o = viewerStartOptions('dark')
    expect(o).toMatchObject({ hide_welcome_screen: true, check_for_updates_on_startup: false, memory_limit: '1.5GB', theme: 'dark', allow_fullscreen: false })
    expect(parseMemoryLimit(String(o.memory_limit))).toBeLessThanOrEqual(VIEWER_MEMORY_LIMIT_MAX)
    expect(parseMemoryLimit('1.5GiB')).toBeGreaterThan(VIEWER_MEMORY_LIMIT_MAX)
    expect(Number.isNaN(parseMemoryLimit('lots'))).toBe(true)
    // 与 Python 侧（catalog viewer_url 的配置）一致
    const py = readFileSync(join(ROOT, 'python/awr/bench/catalog/query.py'), 'utf8')
    expect(py).toContain(`VIEWER_MEMORY_LIMIT = "${VIEWER_MEMORY_LIMIT}"`)
    expect(HIDDEN_PANELS).toEqual(['top', 'selection'])
    const calls: unknown[][] = []
    const panels: [string, unknown][] = []
    let stopped = 0
    class WV {
      start(...a: unknown[]) {
        calls.push(a)
        return Promise.resolve()
      }
      on() {
        return () => {}
      }
      override_panel_state(p: string, s: unknown) {
        panels.push([p, s])
      }
      stop() {
        stopped++
      }
    }
    const parent = { tag: 'div' } as unknown as HTMLElement
    const v = await startViewer(parent, { theme: 'light', load: async () => ({ WebViewer: WV }) as unknown as ViewerModule })
    expect(calls).toHaveLength(1)
    expect(calls[0]![0]).toBeNull()                                   // 不在 start 时打开录制
    expect(calls[0]![1]).toBe(parent)
    expect(calls[0]![2]).toMatchObject({ theme: 'light', memory_limit: '1.5GB' })
    expect(panels).toEqual([['top', 'hidden'], ['selection', 'hidden']])
    v.stop()
    expect(stopped).toBe(1)
    class Bad extends WV {
      start() {
        return Promise.reject(new Error('no simd'))
      }
    }
    await expect(startViewer(parent, { load: async () => ({ WebViewer: Bad }) as unknown as ViewerModule })).rejects.toThrow('no simd')
    expect(stopped).toBe(2)                                           // 启动失败时已释放
  })

  it('counts Wasm bytes only for the viewer module and restores fetch', async () => {
    const body = (n: number) => new Response(new Uint8Array(n), { headers: { 'content-type': 'application/wasm' } })
    const orig = vi.fn(async (u: string) => body(u.endsWith('.wasm') ? 300_000 : 10))
    vi.stubGlobal('fetch', orig)
    const seen: number[] = []
    const got = await withWasmProgress((n) => seen.push(n), async () => {
      const a = await fetch('/assets/re_viewer_bg-Ab12cD.wasm')
      const b = await fetch('/lab-static/index.json')
      return [(await a.arrayBuffer()).byteLength, (await b.arrayBuffer()).byteLength]
    })
    expect(got).toEqual([300_000, 10])
    expect(seen.at(-1)).toBe(300_000)
    expect(globalThis.fetch).toBe(orig)
  })

  it('pins the viewer to 0.38.1 together with rerun-sdk; the progress total is the locked Wasm size', () => {
    const pj = JSON.parse(readFileSync(join(WEB, 'package.json'), 'utf8')) as { dependencies: Record<string, string> }
    expect(pj.dependencies['@rerun-io/web-viewer']).toBe(RERUN_VIEWER_VERSION)
    const lock = JSON.parse(readFileSync(join(ROOT, 'package-lock.json'), 'utf8')) as { packages: Record<string, { version: string }> }
    expect(lock.packages['node_modules/@rerun-io/web-viewer']?.version).toBe(RERUN_VIEWER_VERSION)
    expect(readFileSync(join(ROOT, 'requirements-d3.in'), 'utf8')).toMatch(new RegExp(`^rerun-sdk==${RERUN_VIEWER_VERSION.replace(/\./g, '\\.')}$`, 'm'))
    const pkgDir = [join(WEB, 'node_modules/@rerun-io/web-viewer'), join(ROOT, 'node_modules/@rerun-io/web-viewer')].find((d) => existsSync(d))!
    expect(statSync(join(pkgDir, 're_viewer_bg.wasm')).size).toBe(VIEWER_WASM_BYTES)
    expect((JSON.parse(readFileSync(join(pkgDir, 'package.json'), 'utf8')) as { version: string }).version).toBe(RERUN_VIEWER_VERSION)
  })

  it('registers the viewer licence (BOM and third-party notices)', () => {
    const bom = JSON.parse(readFileSync(join(ROOT, 'tools/ci/bom.json'), 'utf8')) as { items: { name: string; version: string; ecosystem: string; adapterBoundary: string[] }[] }
    const e = bom.items.find((i) => i.ecosystem === 'npm' && i.name === '@rerun-io/web-viewer')
    expect(e?.version).toBe(RERUN_VIEWER_VERSION)
    expect(e?.adapterBoundary).toEqual(['apps/web/src/ui/lab/inspect/viewerLoader.ts'])
    const notices = readFileSync(join(ROOT, 'THIRD_PARTY_NOTICES.md'), 'utf8')
    expect(notices).toMatch(/\| @rerun-io\/web-viewer [^|]*\| 0\.38\.1 \| MIT or Apache-2\.0/)
  })
})

// ── 按需加载与公开构建边界 ────────────────────────────────────────────────────────────────────────────────────────────

function walkSrc(dir: string, out: string[] = []): string[] {
  for (const n of readdirSync(dir)) {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) walkSrc(p, out)
    else if (/\.(ts|tsx|js|mjs)$/.test(n)) out.push(p)
  }
  return out
}

function resolveImport(from: string, spec: string): string | null {
  const base = spec.startsWith('@/') ? join(SRC, spec.slice(2)) : spec.startsWith('.') ? resolve(dirname(from), spec) : null
  if (!base) return null
  for (const c of [base, `${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')]) if (existsSync(c) && statSync(c).isFile()) return c
  return null
}

const STATIC_IMPORT_RE = /^\s*(?:import|export)\s+(?!type\b)(?:[^'";]*?\sfrom\s+)?['"]([^'"]+)['"]/gm

/** 静态导入图（不含仅类型导入与动态 import()） */
function staticGraph(entry: string): Set<string> {
  const seen = new Set<string>()
  const stack = [entry]
  while (stack.length) {
    const f = stack.pop()!
    if (seen.has(f)) continue
    seen.add(f)
    const code = readFileSync(f, 'utf8')
    for (const m of code.matchAll(STATIC_IMPORT_RE)) {
      const r = resolveImport(f, m[1]!)
      if (r) stack.push(r)
      else if (!m[1]!.startsWith('@/') && !m[1]!.startsWith('.')) seen.add(`pkg:${m[1]}`)
    }
  }
  return seen
}

describe('on-demand loading and build boundaries (D3-AC-25; D1-AC-03b not regressed)', () => {
  it('only viewerLoader.ts references the viewer package, and only through dynamic import()', () => {
    const hits = walkSrc(SRC).filter((f) => /['"`]@rerun-io\//.test(readFileSync(f, 'utf8')))
    expect(hits.map((f) => f.slice(SRC.length + 1))).toEqual(['ui/lab/inspect/viewerLoader.ts'])
    const code = readFileSync(hits[0]!, 'utf8')
    expect([...code.matchAll(/['"]@rerun-io\/[^'"]+['"]/g)].length).toBe(1)
    expect(code).toMatch(/import\('@rerun-io\/web-viewer'\)/)
    expect(code).not.toMatch(/^\s*import\s[^(]*['"]@rerun-io\//m)
  })

  it('the D2 sandbox entry and the lab entry pages never reach the viewer package statically', () => {
    for (const entry of ['main.tsx', 'app/lab-public/entry.tsx', 'ui/lab/inspect/InspectPage.tsx']) {
      const g = staticGraph(join(SRC, entry))
      expect([...g].filter((f) => f.includes('@rerun-io')), entry).toEqual([])
    }
  })

  it('the panel can go into the /lab/ public build: no bench client, app router or app dictionary in its static graph', () => {
    const g = [...staticGraph(join(SRC, 'ui/lab/inspect/InspectPanel.tsx'))]
    const files = g.filter((f) => !f.startsWith('pkg:'))
    for (const f of files) {
      expect(LAB_PUBLIC_FORBIDDEN_RE.test(f), f).toBe(false)
      expect(f.endsWith(join('app', 'i18n', 'index.ts')), f).toBe(false)
      expect(f.includes(join('stores', 'lab')), f).toBe(false)
    }
    expect(files.some((f) => f.endsWith('inspectController.ts'))).toBe(true)
  })
})

// ── 路由、文案与外壳 ──────────────────────────────────────────────────────────────────────────────────────────────────

describe('inspect route and copy (AWR-14 §21.2.2, §21.9.2)', () => {
  afterEach(() => setLocale('zh-CN'))

  it('registers lab-inspect at /lab/runs/:rid/inspect with the run id check', async () => {
    const { route } = await import('@/app/routes/lab/inspect.route')
    expect(route).toMatchObject({ id: 'lab-inspect', pattern: '/lab/runs/:rid/inspect', layer: 'overlay' })
    expect(route.validate?.({ rid: 'run-k2m4q7xz3a' })).toBe(true)
    expect(route.validate?.({ rid: '../x' })).toBe(false)
  })

  it('pairs the zh-CN and en shards and keeps the §21.9.2 wording', () => {
    const zh = JSON.parse(readFileSync(join(SRC, 'app/i18n/lab/inspect.zh-CN.json'), 'utf8')) as Record<string, string>
    const en = JSON.parse(readFileSync(join(SRC, 'app/i18n/lab/inspect.en.json'), 'utf8')) as Record<string, string>
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort())
    for (const k of Object.keys(zh)) expect(k.startsWith('lab.inspect.'), k).toBe(true)
    expect(zh['lab.inspect.unsupported.title']).toBe('当前设备不支持数据检视面板')
    expect(zh['lab.inspect.unsupported.software']).toContain('可下载时间窗切片，在 Rerun 原生 Viewer 中打开。')
    expect(zh['lab.inspect.loading']).toBe('正在加载数据检视组件 · {mb} / {total} MB')
    expect(zh['lab.inspect.opening']).toBe('正在打开录制')
    expect(zh['lab.inspect.error.version']).toBe('数据检视未能打开录制（694 RERUN_VERSION_MISMATCH）。该录制需要迁移后查看。')
    expect(zh['lab.inspect.drivenBy']).toBe('由数据检视驱动')
    expect(zh['lab.inspect.native']).toBe('在原生 Viewer 中打开')
    expect(t('lab.inspect.docTitle', { rid: 'run-k2m4q7xz3a' })).toBe('run-k2m4q7xz3a · 数据检视 · 事件沙盘 · ANet Drone4D')
    setLocale('en')
    expect(t('lab.inspect.loading', { mb: '24.4', total: '48.9' })).toBe('Loading data inspection component · 24.4 / 48.9 MB')
    for (const [k, v] of Object.entries(en)) expect(/[一-鿿]/.test(v), k).toBe(false)
  })

  it('offers a stand-alone translator for the /lab/ public build (no application dictionary)', () => {
    expect(inspectText('zh-CN')('lab.inspect.note.static', { version: RERUN_VIEWER_VERSION })).toBe('数据来自公开时间窗切片与底图（public 档，只读）。Rerun 0.38.1')
    expect(inspectText('en')('lab.inspect.download')).toBe('Download slice')
    expect(inspectText('en')('lab.inspect.no.such')).toBe('lab.inspect.no.such')
    const g = [...staticGraph(join(SRC, 'ui/lab/inspect/text.ts'))].filter((f) => !f.startsWith('pkg:'))
    for (const f of g) expect(LAB_PUBLIC_FORBIDDEN_RE.test(f) || f.endsWith(join('app', 'i18n', 'index.ts')), f).toBe(false)
  })

  it('renders the shell states with host copy: unsupported with the fallback action, and idle', () => {
    const zh = JSON.parse(readFileSync(join(SRC, 'app/i18n/lab/inspect.zh-CN.json'), 'utf8')) as Record<string, string>
    const tt = (k: string, p?: Record<string, string | number>) => formatMessage(zh[k] ?? k, p, 'zh-CN')
    const ph = new MemPlayhead()
    const un = renderToStaticMarkup(h(InspectPanel, {
      source: STATIC, playhead: ph, support: { ok: false, reason: 'software' }, t: tt, fallback: h('a', { href: STATIC.slices[0]!.url }, tt('lab.inspect.download')),
    }))
    expect(un).toContain('data-inspect-state="unsupported"')
    expect(un).toContain('当前设备不支持数据检视面板')
    expect(un).toContain('设备为软件渲染（Tier S）')
    expect(un).toContain('下载切片')
    const idle = renderToStaticMarkup(h(InspectPanel, { source: STATIC, playhead: ph, support: { ok: true }, t: tt }))
    expect(idle).toContain('data-inspect-state="idle"')
    expect(idle).toContain('加载数据检视')
    expect(idle).toContain('data-inspect-viewer=""')
  })

  it('renders the internal page: header, catalog label, evaluation switch, native viewer, scene and timeline', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    qc.setQueryData(['lab', 'session'], { principal: 'erin', role: 'evaluator', projects: ['d3'], via: 'cookie' })
    qc.setQueryData(['lab', 'run', REC], {
      run_id: REC, world_id: 'oryahovo-crop1', frames: [],
      fleet: [{ uav: 'uav-01', path: { kind: 'orbit', radius_m: 40, period_s: 60 } }, { uav: 'uav-02', path: { kind: 'orbit', radius_m: 40, period_s: 60, phase_deg: 180 } }],
    })
    const html = renderToStaticMarkup(h(QueryClientProvider, { client: qc }, h(InspectPage, { params: { rid: REC }, search: new URLSearchParams('t=12.5') })))
    expect(html).toContain('data-view="lab-inspect"')
    expect(html).toContain('data-inspect-catalog="dist"')
    expect(html).toContain('数据检视')
    expect(html).toContain('catalog：分发（观测层）')
    expect(html).toContain('打开评测 catalog')
    expect(html).toContain('在原生 Viewer 中打开')
    expect(html).toContain('data-scene-uav="uav-01"')
    expect(html).toContain('data-scene-uav="uav-02"')
    expect(html).toContain('data-inspect-timeline=""')
    expect(html).toContain('数据来自 Rerun catalog（分发 catalog 只含观测层与 Agent 层）。Rerun 0.38.1')
    // Node 中没有画布与布局宽度：面板为 UNSUPPORTED，提供原生 Viewer 入口
    expect(html).toContain('data-inspect-state="unsupported"')
    expect(html).toMatch(/data-inspect-unsupported="(layout|nogpu)"/)
  })
})
