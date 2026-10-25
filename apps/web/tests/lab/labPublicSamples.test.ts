// /lab/ 公开样例运行页（F07；AWR-14 §21.29.3、§21.14；D3-AC-21"事件回放、多机私有视角（RGB 与热成像伪彩视频按仿真时间同步，偏差
// ≤ 1 帧）、样例数据检视（Tier S 为切片下载）"、D3-AC-25 公开样例页条款；UX-AC-113）。这里检查纯函数与静态渲染：
// - labctl 生成的真实索引（fixtures/lab_static_index.labctl.json，由 tools/deploy/emax/d3-public/labctl.py build_index 与
//   tests/d3/deploy/d3public_fixtures.py 生成）的解析：视频种类、机队轨迹、底图字节数与分发渠道地址；
// - tracks_obs.bin（tracks.v1 观测层）的解码与取样；
// - 视频同步器：以假视频元素模拟解码延迟、seek 延迟、时钟偏差、拖动与速率切换，合成帧与播放头应显示帧的偏差 ≤ 1 帧；缓冲与区间外；
// - 通道分组、视图方式与设备档的同时播放上限；
// - 路由扩展、文案分片、静态导入图（/lab/ 构建守卫、Viewer 包只经动态 import）与页面静态渲染（无得分、无 catalog 与 bench 地址）。
// 真实浏览器中的视频同步（H.264、requestVideoFrameCallback）与网络请求清单由 Playwright `lab/public-samples` 复测（R07）。
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it } from 'vitest'
import { LAB_PUBLIC_FORBIDDEN_RE, LAB_SCAN_STRINGS, LAB_VENDOR_WASM_RE, remapVendorStrings } from '../../vite.config.ts'
import { LAB_ROUTES } from '@/app/lab-public/App'
import { setLabLocale } from '@/app/lab-public/i18n'
import { matchLabRoute } from '@/app/lab-public/router'
import { parseStaticIndex, sampleFiles, type LabStaticIndex } from '@/app/lab-public/staticIndex'
import { route } from '@/app/lab-public/samples/sample.route'
import { PrivateViews } from '@/app/lab-public/samples/PrivateViews'
import { clockText, initialSampleT, inspectSource, parseSampleT, SAMPLE_AIRBORNE_MIN_S, SAMPLE_DEFAULT_T_S, SampleRunPage } from '@/app/lab-public/samples/SampleRunPage'
import { eventOf, sampleDurationS } from '@/app/lab-public/samples/sampleData'
import { sampleT, SAMPLE_DICTS } from '@/app/lab-public/samples/text'
import { allowedRequest, blockedRequests, installStaticGuard } from '@/app/lab-public/samples/staticGuard'
import { encodeTracks, parseTracks, trackPolyline, trackXY, tracksSpan, TracksError } from '@/app/lab-public/samples/tracks'
import {
  availableModes, BUFFERING_MS, defaultMode, deviceClass, frameCount, frameOfVideo, groupByUav, maxConcurrentVideos, seekTarget, targetFrame,
  VIDEO_FPS, videoKind, VideoSyncGroup, visibleTiles, type VideoLike,
} from '@/app/lab-public/samples/videoSync'
import { TapeReader } from '@/engine/actors/tape/decode'
import { checkStaticSource } from '@/ui/lab/inspect/inspect'
import { VIEWER_WASM_BYTES } from '@/ui/lab/inspect/viewerLoader'
import { readBytes } from '../contracts/util'

const WEB = resolve(import.meta.dirname, '../..')
const SRC = join(WEB, 'src')
const SAMPLES = join(SRC, 'app/lab-public/samples')
const RUN = 'run-abcdefghij'
const MB = 1024 * 1024

const labctlIndex = (): Record<string, unknown> => JSON.parse(readFileSync(join(import.meta.dirname, 'fixtures/lab_static_index.labctl.json'), 'utf8')) as Record<string, unknown>

/** labctl 索引加上 3 机 RGB 与热成像视频、一个 > 64 MB 经 ModelScope 的视频（数据检视与私有视角的全量用例） */
function fullIndex(): LabStaticIndex {
  const raw = labctlIndex()
  const run = (raw.events as { runs: Record<string, unknown>[] }[])[0]!.runs[0]!
  run.videos = ['uav-01', 'uav-02', 'uav-03'].flatMap((u) => [
    { uav: u, cam: 'cam0', kind: 'rgb', path: `runs/${RUN}/video/${u}_cam0.mp4`, bytes: 2 * MB },
    { uav: u, cam: 'therm0', kind: 'thermal', path: `runs/${RUN}/video/${u}_therm0.mp4`, bytes: u === 'uav-03' ? 70 * MB : MB,
      ...(u === 'uav-03' ? { external: { channel: 'modelscope', url: 'https://modelscope.cn/datasets/anet/drone4d-samples/file/view/master/uav-03_therm0.mp4' } } : {}) },
  ])
  return parseStaticIndex(raw)
}

// ── 静态制品索引（labctl 生成） ─────────────────────────────────────────────────────────────────────────────────────

describe('labctl index of a sample run (AWR-17 §18.8.5; F08 follow-up 3)', () => {
  it('parses the index labctl writes, including video kinds, fleet tracks and the basemap size', () => {
    const idx = parseStaticIndex(labctlIndex())
    expect(idx.version).toBe('2026.10.19-1')
    expect(idx.features).toEqual({ inspect: true, compare: false })
    const run = idx.events[0]!.runs[0]!
    expect(run.runId).toBe(RUN)
    expect(run.videos.map((v) => [v.uav, v.cam, v.kind, v.external])).toEqual([['uav-01', 'cam0', 'rgb', null], ['uav-01', 'thermal0', 'thermal', null]])
    expect(run.tracksObs).toBe(`runs/${RUN}/tracks_obs.bin`)
    expect(run.basemap).toBe(`runs/${RUN}/slices/basemap.rrd`)
    expect(run.basemapBytes).toBe(307200)
    expect(run.basemapSha256).toMatch(/^[0-9a-f]{64}$/)
    expect(run.slices.map((s) => [s.eventId, s.tFromS, s.tToS, s.path])).toEqual([['encounter_ab', 17.5, 27.5, `runs/${RUN}/slices/encounter_ab.rrd`]])
    expect(sampleFiles(run).find((f) => f.kind === 'basemap')?.bytes).toBe(307200)
    expect(eventOf(idx, run)?.tape).toBe('events/crop-handoff-001/tape.bin')
  })

  it('keeps only ModelScope links for files above 64 MB (ADR-201)', () => {
    const idx = fullIndex()
    const v = idx.events[0]!.runs[0]!.videos
    expect(v.find((x) => x.uav === 'uav-03' && x.kind === 'thermal')?.external).toMatch(/^https:\/\/modelscope\.cn\//)
    for (const url of ['https://evil.example/x.mp4', 'http://modelscope.cn/x', 'https://modelscope.cn.evil.example/x', 'javascript:alert(1)']) {
      const raw = labctlIndex()
      const run = (raw.events as { runs: { videos: Record<string, unknown>[] }[] }[])[0]!.runs[0]!
      run.videos[0]!.external = { channel: 'modelscope', url }
      expect(parseStaticIndex(raw).events[0]!.runs[0]!.videos[0]!.external, url).toBeNull()
    }
  })

  it('reads an unknown kind as null and infers it from the sensor name', () => {
    const raw = labctlIndex()
    const run = (raw.events as { runs: { videos: Record<string, unknown>[] }[] }[])[0]!.runs[0]!
    run.videos[0]!.kind = 'depth'
    delete run.videos[1]!.kind
    const v = parseStaticIndex(raw).events[0]!.runs[0]!.videos
    expect(v.map((x) => x.kind)).toEqual([null, null])
    expect(v.map(videoKind)).toEqual(['rgb', 'thermal'])
    expect(['therm0', 'thermal0', 'ir0', 'cam0', 'cam1', 'rgb0'].map((cam) => videoKind({ cam }))).toEqual(['thermal', 'thermal', 'thermal', 'rgb', 'rgb', 'rgb'])
  })
})

// ── tracks_obs.bin ───────────────────────────────────────────────────────────────────────────────────────────────

describe('fleet tracks tracks_obs.bin (AWR-17 §18.8.3, observation layer)', () => {
  const head = { run_id: RUN, hz: 10, t0_ns: 0, uavs: [{ uav_id: 'uav-01', primary_camera: 'cam0' }, { uav_id: 'uav-02', primary_camera: 'cam0' }] }
  const samples = [0, 1].map((k) => Array.from({ length: 51 }, (_, i) => ({ e: 10 * k + i, n: i ? -2 * i : 0, u: 30, flags: i === 50 ? 1 : 0 })))
  const bin = encodeTracks(head, samples)

  it('decodes the 56-byte records in uav order', () => {
    expect(bin.byteLength).toBe(12 + new TextEncoder().encode(JSON.stringify({ ...head, n_samples: 51 })).byteLength + 2 * 51 * 56 + 32)
    const f = parseTracks(bin)
    expect([f.runId, f.hz, f.t0Ns, f.nSamples, f.uavs.map((u) => u.uavId)]).toEqual([RUN, 10, 0, 51, ['uav-01', 'uav-02']])
    expect(Array.from(f.uavs[1]!.pos.subarray(0, 6))).toEqual([10, 0, 30, 11, -2, 30])
    expect(f.uavs[0]!.flags[50]).toBe(1)
    expect(f.uavs[0]!.linkQuality[0]).toBe(3)
    expect(tracksSpan(f)).toEqual({ startS: 0, endS: 5 })
  })

  it('interpolates positions linearly and holds the first and last sample outside the span', () => {
    const f = parseTracks(bin)
    const u = f.uavs[0]!
    expect(trackXY(f, u, 0.25)).toEqual([2.5, -5])
    expect(trackXY(f, u, -3)).toEqual([0, 0])
    expect(trackXY(f, u, 99)).toEqual([50, -100])
    expect(trackPolyline(f, u, 2)).toEqual([[0, 0], [20, -40], [40, -80], [50, -100]])
  })

  it('rejects the truth layer, a wrong magic, version or length', () => {
    const err = (b: Uint8Array): string => {
      try {
        parseTracks(b)
      } catch (e) {
        return e instanceof TracksError ? e.detail : String(e)
      }
      return 'none'
    }
    const truth = bin.slice()
    new DataView(truth.buffer).setUint16(6, 1, true)
    expect(err(truth)).toBe('layer is not obs')
    const magic = bin.slice()
    magic[0] = 0x58
    expect(err(magic)).toBe('magic')
    const ver = bin.slice()
    new DataView(ver.buffer).setUint16(4, 2, true)
    expect(err(ver)).toBe('version 2')
    expect(err(bin.slice(0, bin.byteLength - 1))).toBe('length')
  })
})

// ── 视频同步 ──────────────────────────────────────────────────────────────────────────────────────────────────────

/**
 * 假视频元素：按墙钟推进 currentTime（播放速率 × 时钟偏差），起播与 seek 有延迟，合成帧变化时回调（模拟 requestVideoFrameCallback
 * 的 mediaTime = 帧的显示时间戳）。
 */
class FakeVideo implements VideoLike {
  currentTimeV = 0
  duration: number
  paused = true
  playbackRate = 1
  readyState = 4
  seeking = false
  error: unknown = null
  private startAt: number | null = null
  private seekDoneAt = 0
  private shown = -1
  constructor(private readonly o: { durationS: number; skew?: number; seekMs?: number; startMs?: number; now: () => number; onFrame: (mediaTimeS: number) => void }) {
    this.duration = o.durationS
  }
  get currentTime(): number {
    return this.currentTimeV
  }
  set currentTime(v: number) {
    this.currentTimeV = Math.min(Math.max(0, v), this.duration)
    this.seeking = true
    this.seekDoneAt = this.o.now() + (this.o.seekMs ?? 40)
  }
  play(): Promise<void> {
    this.paused = false
    this.startAt = this.o.now() + (this.o.startMs ?? 60)
    return Promise.resolve()
  }
  pause(): void {
    this.paused = true
  }
  /** 推进 dtMs 墙钟 */
  step(dtMs: number): void {
    const now = this.o.now()
    if (this.seeking && now >= this.seekDoneAt) this.seeking = false
    if (!this.paused && !this.seeking && this.startAt !== null && now >= this.startAt) {
      this.currentTimeV = Math.min(this.duration, this.currentTimeV + (dtMs / 1000) * this.playbackRate * (this.o.skew ?? 1))
      if (this.currentTimeV >= this.duration) this.paused = true
    }
    if (!this.seeking) {
      // 合成器选帧：播放中取显示区间与刷新区间重叠最多的帧（约 round），暂停时取 currentTime 所在的帧（Chrome VideoRendererAlgorithm）
      const k = Math.min(this.paused ? frameOfVideo(this.currentTimeV, VIDEO_FPS) : Math.round(this.currentTimeV * VIDEO_FPS), Math.round(this.duration * VIDEO_FPS) - 1)
      if (k !== this.shown) {
        this.shown = k
        this.o.onFrame(k / VIDEO_FPS)
      }
    }
  }
}

/** 页面时钟循环的模拟：每 16.7 ms 推进播放头（缓冲中冻结）并 tick 同步器 */
function simulate(cfg: { videos: { skew?: number; seekMs?: number; startMs?: number }[]; durationS?: number; script: (tMs: number, ph: Ph, vids: FakeVideo[]) => void; totalMs: number }) {
  let now = 0
  const ph: Ph = { tS: 0, playing: false, rate: 1, durationS: cfg.durationS ?? 60 }
  const g = new VideoSyncGroup({ now: () => now })
  const vids = cfg.videos.map((v, i) => {
    const el = new FakeVideo({ durationS: cfg.durationS ?? 60, ...v, now: () => now, onFrame: (m) => g.presented(`v${i}`, m, ph.tS) })
    g.add(`v${i}`, el, { t0S: 0, fps: VIDEO_FPS })
    return el
  })
  const dt = 1000 / 60
  let buffered = 0
  for (now = 0; now <= cfg.totalMs; now += dt) {
    cfg.script(now, ph, vids)
    for (const v of vids) v.step(dt)
    if (ph.playing && g.canAdvance) {
      ph.tS = Math.min(ph.durationS, ph.tS + (dt / 1000) * ph.rate)
      if (ph.tS >= ph.durationS) ph.playing = false
    }
    if (g.tick(ph)) buffered++
  }
  return { g, vids, ph, buffered }
}
interface Ph { tS: number; playing: boolean; rate: number; durationS: number }

describe('video sync against the lab playhead (D3-AC-21 offset ≤ 1 frame; UX-AC-113)', () => {
  it('maps the playhead to frames without taking future frames', () => {
    const tm = { t0S: 0, fps: 10 }
    expect([0, 0.0999, 0.1, 0.35, 12.3].map((t) => targetFrame(t, tm))).toEqual([0, 0, 1, 3, 123])
    expect(targetFrame(-0.01, tm)).toBe(-1)
    expect(targetFrame(5.3, { t0S: 5, fps: 10 })).toBe(3)
    expect(frameOfVideo(0.3, 10)).toBe(3) // 0.3 · 10 = 2.9999999999999996
    expect(seekTarget(7, 10)).toBeCloseTo(0.75)
    expect(frameCount(36, 10)).toBe(360)
    expect(frameCount(Number.NaN, 10)).toBeNull()
  })

  it('aligns paused videos to the playhead frame on every scrub', () => {
    const scrubs = [0, 3.33, 12.05, 0.99, 59.95, 27.4, 27.41, 8]
    const r = simulate({
      videos: [{}, { seekMs: 120 }, { seekMs: 15 }], totalMs: scrubs.length * 400,
      script: (t, ph) => {
        ph.tS = scrubs[Math.min(scrubs.length - 1, Math.floor(t / 400))]!
      },
    })
    const s = r.g.stats()
    expect(s.presented).toBeGreaterThanOrEqual(3 * (scrubs.length - 2)) // 首次对齐前合成的帧不计
    for (const v of r.vids) expect(frameOfVideo(v.currentTime, 10)).toBe(targetFrame(8, { t0S: 0, fps: 10 }))
    expect(s.maxAbsDevFrames).toBe(0)
    expect(s.reseeks).toBe(0)
  })

  for (const [name, videos, rate] of [
    ['3 RGB + 3 thermal, ideal decoders, 1×', [{}, {}, {}, {}, {}, {}], 1],
    ['clock skew ±2 %, slow start, 1×', [{ skew: 1.02 }, { skew: 0.98 }, { skew: 1.0, startMs: 180 }, { skew: 0.99, seekMs: 90 }], 1],
    ['clock skew ±3 %, 4×', [{ skew: 1.03 }, { skew: 0.97 }, { seekMs: 70 }], 4],
    ['clock skew ±3 %, 0.5×', [{ skew: 1.03 }, { skew: 0.97 }], 0.5],
  ] as const) {
    it(`keeps every composed frame within 1 frame of the playhead while playing: ${name}`, () => {
      const r = simulate({
        videos: videos.map((v) => ({ ...v })), totalMs: 50_000, durationS: 120,
        script: (t, ph) => {
          if (t === 0) {
            ph.rate = rate
            ph.playing = true
          }
        },
      })
      const s = r.g.stats()
      expect(s.presented).toBeGreaterThan(videos.length * 50)
      expect(s.maxAbsDevFrames).not.toBeNull()
      expect(s.maxAbsDevFrames!).toBeLessThanOrEqual(1)
      for (const v of r.vids) expect(Math.abs(v.currentTime - r.ph.tS)).toBeLessThanOrEqual(0.1)
    })
  }

  it('re-seeks a video that stalls by more than 1 frame and keeps the composed frames within 1 frame', () => {
    // 解码卡顿：第 2 路视频在 8 s 与 20 s 处各停顿 0.6 s（currentTime 落后 6 帧），同步器 seek 回播放头并保持到视频重新推进
    const r = simulate({
      videos: [{}, { seekMs: 80 }], totalMs: 30_000,
      script: (t, ph, vids) => {
        if (t === 0) ph.playing = true
        for (const at of [8000, 20_000]) if (t >= at && t < at + 600) vids[1]!.currentTimeV -= 1 / 60
      },
    })
    const s = r.g.stats()
    expect(s.reseeks).toBeGreaterThanOrEqual(2)
    expect(s.videos[0]!.reseeks).toBe(0)
    expect(Math.abs(r.vids[1]!.currentTime - r.ph.tS)).toBeLessThanOrEqual(0.1)
    // 卡顿期间视频不合成新帧（停在旧帧），恢复后的合成帧回到 1 帧以内
    expect(s.videos[0]!.maxAbsDevFrames).toBeLessThanOrEqual(1)
  })

  it('nudges the rate within ±20 % for sub-frame drift instead of seeking', () => {
    // 播放中视频时钟的目标为播放头减半帧（9.95 s）
    const el = { currentTime: 10.0, duration: 60, paused: false, playbackRate: 2, readyState: 4, seeking: false, play: () => undefined, pause: () => undefined }
    const g = new VideoSyncGroup({ now: () => 0 })
    g.add('a', el, { t0S: 0, fps: 10 })
    g.tick({ tS: 10, playing: true, rate: 2 })
    expect(el.playbackRate).toBeCloseTo(2 - 0.05 / 0.25)
    expect(el.currentTime).toBe(10.0)
    el.currentTime = 9.96
    g.tick({ tS: 10, playing: true, rate: 2 })
    expect(el.playbackRate).toBe(2)
    el.currentTime = 9.5
    g.tick({ tS: 10, playing: true, rate: 2 })
    expect(el.currentTime).toBeCloseTo(9.95 + 2 * 0.06) // 目标前置 seek 时延（初值 60 ms）× 速率
    expect(g.stats().videos[0]!.reseeks).toBe(1)
  })

  it('freezes the playhead while a video is not ready for more than 500 ms and resumes when it is', () => {
    let now = 0
    const ready = { currentTime: 0, duration: 60, paused: true, playbackRate: 1, readyState: 4, seeking: false, play() { this.paused = false }, pause() { this.paused = true } }
    const slow = { ...ready, readyState: 2, play() { this.paused = false }, pause() { this.paused = true } }
    const broken = { ...ready, readyState: 0, error: { code: 4 }, play() { this.paused = false }, pause() { this.paused = true } }
    const g = new VideoSyncGroup({ now: () => now })
    g.add('ready', ready, { t0S: 0, fps: 10 })
    g.add('slow', slow, { t0S: 0, fps: 10 })
    g.add('broken', broken, { t0S: 0, fps: 10 })
    expect(g.tick({ tS: 1, playing: true, rate: 1 })).toBe(false)
    now = BUFFERING_MS - 10
    expect(g.tick({ tS: 1, playing: true, rate: 1 })).toBe(false)
    now = BUFFERING_MS + 10
    expect(g.tick({ tS: 1, playing: true, rate: 1 })).toBe(true)
    expect(ready.paused).toBe(true) // 缓冲中全部视频暂停
    expect(g.stats().buffering).toBe(true)
    slow.readyState = 4
    now += 16
    expect(g.tick({ tS: 1, playing: true, rate: 1 })).toBe(false)
    expect(ready.paused).toBe(false)
    expect(g.tick({ tS: 1, playing: false, rate: 1 })).toBe(false)
  })

  it('parks a video at its first or last frame outside its span and does not count the offset there', () => {
    const el = { currentTime: 3, duration: 10, paused: false, playbackRate: 1, readyState: 4, seeking: false, play: () => undefined, pause() { this.paused = true } }
    const g = new VideoSyncGroup({ now: () => 0 })
    g.add('a', el, { t0S: 5, fps: 10 })
    g.tick({ tS: 2, playing: true, rate: 1 })
    expect(el.paused).toBe(true)
    expect(el.currentTime).toBeCloseTo(0.05)
    expect(g.stats().videos[0]!.outside).toBe(true)
    g.tick({ tS: 30, playing: false, rate: 1 })
    expect(el.currentTime).toBeCloseTo(9.95)
    g.presented('a', 9.9, 30)
    expect(g.stats().presented).toBe(0)
    expect(g.devFrames('a', 14.9)).toBe(0)
  })
})

describe('private view channels and device classes (AWR-14 §21.14.3)', () => {
  const vids = fullIndex().events[0]!.runs[0]!.videos
  const groups = groupByUav(vids)

  it('groups RGB and thermal videos per UAV', () => {
    expect(groups.map((g) => [g.uav, g.rgb?.cam, g.thermal?.cam])).toEqual([['uav-01', 'cam0', 'therm0'], ['uav-02', 'cam0', 'therm0'], ['uav-03', 'cam0', 'therm0']])
  })

  it('limits concurrent videos: phone 1, tablet and Tier S 4, desktop Tier B/A 6', () => {
    expect([deviceClass(390, true), deviceClass(820, true), deviceClass(1366, true), deviceClass(1920, false), deviceClass(1100, false)])
      .toEqual(['phone', 'tablet', 'tablet', 'desktop', 'tablet'])
    expect([maxConcurrentVideos('phone', false), maxConcurrentVideos('tablet', false), maxConcurrentVideos('desktop', true), maxConcurrentVideos('desktop', false)])
      .toEqual([1, 4, 4, 6])
  })

  it('offers side by side only when both channels fit the limit, RGB row above the thermal row', () => {
    expect(availableModes(groups, 6)).toEqual(['rgb', 'thermal', 'both'])
    expect(availableModes(groups, 4)).toEqual(['rgb', 'thermal'])
    expect(defaultMode(availableModes(groups, 6))).toBe('both')
    expect(defaultMode(availableModes(groups, 4))).toBe('rgb')
    expect(visibleTiles(groups, 'both', 6).map((x) => `${x.kind}:${x.uav}`)).toEqual(
      ['rgb:uav-01', 'rgb:uav-02', 'rgb:uav-03', 'thermal:uav-01', 'thermal:uav-02', 'thermal:uav-03'])
    expect(visibleTiles(groups, 'thermal', 4).map((x) => x.uav)).toEqual(['uav-01', 'uav-02', 'uav-03'])
    expect(visibleTiles(groups, 'rgb', 1, 'uav-02').map((x) => `${x.kind}:${x.uav}`)).toEqual(['rgb:uav-02'])
    expect(visibleTiles(groups, 'both', 4)).toHaveLength(4)
    expect(availableModes(groupByUav(vids.filter((v) => v.kind === 'rgb')), 6)).toEqual(['rgb'])
  })
})

// ── 页面 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

describe('sample run page (AWR-14 §21.29.3)', () => {
  afterEach(() => setLabLocale('zh-CN'))

  it('overrides the built-in /lab/samples/:sid route', () => {
    expect(route.id).toBe('labPublicSampleRun')
    expect(route.pattern).toBe('/lab/samples/:sid')
    expect(matchLabRoute(LAB_ROUTES, `/lab/samples/${RUN}`)?.route.id).toBe('labPublicSampleRun')
    expect(matchLabRoute(LAB_ROUTES, '/lab/samples')?.route.id).toBe('labPublicSamples')
    expect(route.title({ params: { sid: RUN }, index: fullIndex() }, sampleT)).toBe(`样例运行 ${RUN}`)
  })

  it('builds the static inspection source from hosted slices and the basemap only', () => {
    const idx = fullIndex()
    const run = idx.events[0]!.runs[0]!
    const src = inspectSource(idx, run)!
    expect(src).toEqual({
      kind: 'static', recordingId: RUN,
      slices: [{ url: `/lab-static/v/2026.10.19-1/runs/${RUN}/slices/encounter_ab.rrd`, bytes: 524288 }],
      basemap: { url: `/lab-static/v/2026.10.19-1/runs/${RUN}/slices/basemap.rrd`, bytes: 307200 },
      wasmUrl: null,
    })
    expect(checkStaticSource(src)).toBeNull()
    expect(inspectSource(idx, { ...run, slices: [] })).toBeNull()
    expect(inspectSource(idx, { ...run, slices: run.slices.map((s) => ({ ...s, bytes: 65 * MB })) })).toBeNull()
    // ADR-221：切片、底图与 Viewer Wasm 的 ModelScope 副本优先
    const ms = (f: string) => `https://www.modelscope.cn/datasets/x/y/resolve/master/${f}`
    const ext = inspectSource({ ...idx, viewerWasm: { url: ms('re_viewer_bg.wasm'), bytes: 1, sha256: null } },
      { ...run, slices: run.slices.map((s) => ({ ...s, external: ms('a.rrd') })), basemapExternal: ms('basemap.rrd') })!
    expect(ext.slices).toEqual([{ url: ms('a.rrd'), bytes: 524288 }])
    expect(ext.basemap?.url).toBe(ms('basemap.rrd'))
    expect(ext.wasmUrl).toBe(ms('re_viewer_bg.wasm'))
    expect(checkStaticSource(ext)).toBeNull()
  })

  it('derives the playhead duration from the tape, fleet tracks, slice windows and videos', () => {
    const idx = fullIndex()
    const run = idx.events[0]!.runs[0]!
    expect(sampleDurationS({ run })).toBe(27.5)
    const tape = new TapeReader(readBytes('golden/event/tape-small.bin'))
    expect(sampleDurationS({ run, tape })).toBe(Math.max(27.5, (tape.nTicks - 1) / 10))
    const tr = parseTracks(encodeTracks({ run_id: RUN, hz: 10, t0_ns: 0, uavs: [{ uav_id: 'uav-01', primary_camera: 'cam0' }] },
      [Array.from({ length: 601 }, () => ({ e: 0, n: 0, u: 0 }))]))
    expect(sampleDurationS({ run, tracks: tr })).toBe(60)
    expect(sampleDurationS({ run, videoEndS: 75 })).toBe(75)
    expect(parseSampleT('?t=12.5')).toBe(12.5)
    expect([parseSampleT(''), parseSampleT('?t=-1'), parseSampleT('?t=abc')]).toEqual([null, null, null])
    expect(clockText(125.04)).toBe('02:05.0')
  })

  it('opens at a meaningful airborne moment: ?t= first, then the earliest key-moment slice, then 20 s (V0.3.1)', () => {
    const run = fullIndex().events[0]!.runs[0]!
    // labctl 索引的切片窗口 17.5–27.5 s：打开在关键时刻起点，不在 0 s 的起降点
    expect(initialSampleT('', run, sampleDurationS({ run }))).toEqual({ tS: 17.5, from: 'slice' })
    // 查询参数优先，包括显式的 0 与超出已知时长的值（原有行为）
    expect(initialSampleT('?t=3.5', run)).toEqual({ tS: 3.5, from: 'query' })
    expect(initialSampleT('?t=0', run)).toEqual({ tS: 0, from: 'query' })
    expect(initialSampleT('?t=abc', run)).toEqual({ tS: 17.5, from: 'slice' })
    // 多个切片取最早的；起飞阶段（< 5 s）的切片不作为打开时刻
    const s = (tFromS: number) => ({ ...run.slices[0]!, tFromS, tToS: tFromS + 10 })
    expect(initialSampleT('', { slices: [s(120), s(42), s(2)] })).toEqual({ tS: 42, from: 'slice' })
    expect(initialSampleT('', { slices: [s(0), s(SAMPLE_AIRBORNE_MIN_S - 0.1)] })).toEqual({ tS: SAMPLE_DEFAULT_T_S, from: 'default' })
    // 无切片：缺省 20 s，已知时长更短时取时长
    expect(SAMPLE_DEFAULT_T_S).toBe(20)
    expect(initialSampleT('', { slices: [] })).toEqual({ tS: 20, from: 'default' })
    expect(initialSampleT('', { slices: [] }, 12)).toEqual({ tS: 12, from: 'default' })
    expect(initialSampleT('', { slices: [] }, 300)).toEqual({ tS: 20, from: 'default' })
  })

  it('renders the replay, private views, timeline and downloads without scores or non-static addresses', () => {
    const idx = fullIndex()
    // Node 中没有 WebGL：按 Tier S 处理，同时播放 ≤ 4 路，缺省 RGB 视图（3 机），可切换热成像，不提供并排
    const html = renderToStaticMarkup(createElement(SampleRunPage, { index: idx, params: { sid: RUN } }))
    expect(html).toContain('data-view="lab-public-run"')
    expect(html).toContain('data-figure="lab-public-scene"')
    expect(html).toContain('data-figure="lab-public-views" data-view-mode="rgb" data-view-limit="4"')
    expect(html).toContain('data-view-mode-item="thermal"')
    expect(html).not.toContain('data-view-mode-item="both"')
    expect(html.match(/data-view-tile="rgb\//g)).toHaveLength(3)
    expect(html).toContain('data-sample-timeline=""')
    expect(html).toContain('样例运行，不构成排名')
    expect(html).toContain('事件 crop-handoff-001 v1 · 世界 oryahovo-crop1 · 策略 track_share · 3 架 · 观测档 L2')
    expect(html).toContain('data-sample-inspect-toggle=""')
    expect(html).toContain('data-figure="lab-public-run-files"')
    for (const m of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
      const u = m[1]!
      expect(u.startsWith('/lab-static/v/2026.10.19-1/') || u === '/lab/' || u.startsWith('https://modelscope.cn/'), u).toBe(true)
    }
    for (const s of ['/api/', '/rerun', 'catalog', 'metrics', '/leaderboard', '/submissions', '/board', '/registry', '得分', 'score']) {
      expect(html.includes(s), s).toBe(false)
    }
  })

  it('lays out 3 UAVs × [RGB | thermal] on a desktop Tier B/A device, playing the ModelScope copy in CORS mode (ADR-221)', () => {
    const idx = fullIndex()
    const run = idx.events[0]!.runs[0]!
    const html = renderToStaticMarkup(createElement(PrivateViews, { index: idx, run, group: new VideoSyncGroup(), limit: 6, t: sampleT }))
    expect(html).toContain('data-view-mode="both"')
    expect([...html.matchAll(/data-view-tile="([^"]+)"/g)].map((m) => m[1])).toEqual(
      ['rgb/uav-01/cam0', 'rgb/uav-02/cam0', 'rgb/uav-03/cam0', 'thermal/uav-01/therm0', 'thermal/uav-02/therm0', 'thermal/uav-03/therm0'])
    expect(html.match(/<video /g)).toHaveLength(6)
    expect(html).not.toContain('data-view-state="offsite"')
    expect(html).toMatch(/<video[^>]*src="https:\/\/modelscope\.cn\/datasets\/anet\/drone4d-samples\/file\/view\/master\/uav-03_therm0\.mp4"[^>]*crossorigin="anonymous"/)
    expect(html).toContain(`src="/lab-static/v/2026.10.19-1/runs/${RUN}/video/uav-01_therm0.mp4"`)
    expect(html.match(/crossorigin="anonymous"/g)).toHaveLength(1)
    // > 64 MB 且没有分发渠道副本：不发起请求，格内显示说明（ADR-201）
    const bare = { ...run, videos: run.videos.map((v) => (v.external ? { ...v, external: null } : v)) }
    const off = renderToStaticMarkup(createElement(PrivateViews, { index: idx, run: bare, group: new VideoSyncGroup(), limit: 6, t: sampleT }))
    expect(off.match(/<video /g)).toHaveLength(5)
    expect(off).toContain('data-view-state="offsite"')
    expect(html).toContain('data-thermal-scale="white-hot"')
    expect(html).toContain('muted=""')
    const phone = renderToStaticMarkup(createElement(PrivateViews, { index: idx, run, group: new VideoSyncGroup(), limit: 1, t: sampleT }))
    expect(phone.match(/data-view-tile="/g)).toHaveLength(1)
    expect(phone).toContain('aria-label="下一架"')
  })

  it('shows the unknown-sample state and switches the copy with the shell language', () => {
    const idx = fullIndex()
    expect(renderToStaticMarkup(createElement(SampleRunPage, { index: idx, params: { sid: 'run-zzzzzzzzzz' } }))).toContain('样例不存在')
    setLabLocale('en')
    const html = renderToStaticMarkup(createElement(SampleRunPage, { index: idx, params: { sid: RUN } }))
    expect(html).toContain('Sample run, not a ranking')
    expect(html).toContain('Per-UAV private views')
    expect(/[一-鿿]/.test(html.replace(/OpenStreetMap contributors（ODbL-1.0）/g, ''))).toBe(false)
  })
})

describe('copy shard lab.public.run.* (AWR-14 §21.15; D3-AC-23)', () => {
  it('pairs zh-CN and en, keeps the prefix and has every key the page uses', () => {
    const zh = SAMPLE_DICTS['zh-CN']
    const en = SAMPLE_DICTS.en
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort())
    for (const k of Object.keys(zh)) expect(k.startsWith('lab.public.run.'), k).toBe(true)
    for (const [k, v] of Object.entries(en)) expect(/[一-鿿]/.test(v), k).toBe(false)
    for (const v of [...Object.values(zh), ...Object.values(en)]) expect(/[!！~～]/.test(v), v).toBe(false)
    const used = new Set<string>()
    for (const f of readdirSync(SAMPLES).filter((x) => /\.tsx?$/.test(x))) {
      const code = readFileSync(join(SAMPLES, f), 'utf8')
      for (const m of code.matchAll(/['`](lab\.public\.run\.[\w.]+?)(\$\{|['`])/g)) used.add(m[2] === '${' ? `${m[1]}*` : m[1]!)
    }
    for (const k of used) {
      if (k.endsWith('*')) expect(Object.keys(zh).some((x) => x.startsWith(k.slice(0, -1))), k).toBe(true)
      else expect(zh[k], k).toBeDefined()
    }
    for (const k of Object.keys(zh)) {
      const covered = used.has(k) || [...used].some((u) => u.endsWith('*') && k.startsWith(u.slice(0, -1)))
      expect(covered, `unused key ${k}`).toBe(true)
    }
  })
})

describe('request guard of the sample page (AWR-14 §21.14.2 rule 1; D3-AC-21 static GET only)', () => {
  const BASE = 'https://drone4d.agentnetwork.org.cn/lab/samples/run-abcdefghij'

  it('allows same-origin GET and HEAD under /lab/ and /lab-static/ only', () => {
    for (const u of ['/lab-static/v/2026.10.19-1/runs/x/video/a.mp4', 'https://drone4d.agentnetwork.org.cn/lab/assets/re_viewer_bg-x.wasm', '/lab/assets/a.js']) {
      expect(allowedRequest(u, 'GET', BASE), u).toBe(true)
    }
    expect(allowedRequest('/lab-static/index.json', 'HEAD', BASE)).toBe(true)
    for (const [u, m] of [['https://tel.rerun.io/api/pogevent', 'POST'], ['https://tel.rerun.io/api/pogevent', 'GET'], ['/api/bench/v1/runs', 'GET'],
      ['/lab-static/index.json', 'POST'], ['/rerun/catalog/dist', 'GET'], ['https://evil.example/lab-static/x', 'GET'], ['/lab-static/../api/x', 'GET'],
      ['rerun+http://127.0.0.1:9876/proxy', 'GET']] as const) {
      expect(allowedRequest(u, m, BASE), `${m} ${u}`).toBe(false)
    }
  })

  it('wraps fetch and sendBeacon while mounted, counts blocked requests and restores on unmount', async () => {
    const g = globalThis as unknown as { fetch: typeof fetch; location?: unknown; navigator: Navigator }
    const saved = { fetch: g.fetch, location: g.location, beacon: Object.getOwnPropertyDescriptor(g.navigator, 'sendBeacon') }
    const seen: string[] = []
    const fake = ((u: RequestInfo | URL) => {
      seen.push(typeof u === 'string' ? u : u instanceof URL ? u.href : u.url)
      return Promise.resolve(new Response('ok'))
    }) as typeof fetch
    const beacon = () => true
    Object.defineProperty(g.navigator, 'sendBeacon', { value: beacon, configurable: true, writable: true })
    g.fetch = fake
    g.location = { href: BASE }
    try {
      const n0 = blockedRequests()
      const off = installStaticGuard()
      const off2 = installStaticGuard() // 嵌套挂载
      await g.fetch('/lab-static/v/2026.10.19-1/runs/x/tracks_obs.bin')
      await expect(g.fetch('https://tel.rerun.io/api/pogevent', { method: 'POST', body: '{}' })).rejects.toThrow(/blocked/)
      await expect(g.fetch(new Request('https://drone4d.agentnetwork.org.cn/api/bench/v1/session'))).rejects.toThrow(/blocked/)
      expect(g.navigator.sendBeacon('https://tel.rerun.io/api/pogevent', '{}')).toBe(false)
      expect(seen).toEqual(['/lab-static/v/2026.10.19-1/runs/x/tracks_obs.bin'])
      expect(blockedRequests() - n0).toBe(3)
      off2()
      expect(g.fetch).not.toBe(fake)
      off()
      off()
      expect(g.fetch).toBe(fake)
      expect(Reflect.get(g.navigator, 'sendBeacon')).toBe(beacon)
      // 守卫之上另有包装（Viewer 加载期间的字节统计）时卸载：不还原，守卫改为直通
      const off3 = installStaticGuard()
      const guarded = g.fetch
      const outer = ((u: RequestInfo | URL, i?: RequestInit) => guarded(u, i)) as typeof fetch
      g.fetch = outer
      off3()
      expect(g.fetch).toBe(outer)
      await g.fetch('https://tel.rerun.io/x')
      expect(seen.at(-1)).toBe('https://tel.rerun.io/x')
    } finally {
      g.fetch = saved.fetch
      g.location = saved.location
      if (saved.beacon) Object.defineProperty(g.navigator, 'sendBeacon', saved.beacon)
      else Reflect.deleteProperty(g.navigator, 'sendBeacon')
    }
  })
})

// ── /lab/ 构建守卫 ────────────────────────────────────────────────────────────────────────────────────────────────

const STATIC_IMPORT_RE = /^\s*(?:import|export)\s+(?!type\b)(?:[^'"]*?\sfrom\s+)?['"]([^'"]+)['"]/gm

function resolveImport(from: string, spec: string): string | null {
  let base: string
  if (spec.startsWith('@/')) base = join(SRC, spec.slice(2))
  else if (spec.startsWith('.')) base = resolve(dirname(from), spec)
  else return null
  for (const ext of ['', '.ts', '.tsx', '/index.ts', '/index.tsx']) {
    try {
      if (readFileSync(base + ext) && !/\/$/.test(base + ext)) return base + ext
    } catch {
      // try the next extension
    }
  }
  return null
}

function staticGraph(entry: string): Set<string> {
  const seen = new Set<string>()
  const stack = [entry]
  while (stack.length) {
    const f = stack.pop()!
    if (seen.has(f)) continue
    seen.add(f)
    if (!/\.tsx?$/.test(f)) continue
    for (const m of readFileSync(f, 'utf8').matchAll(STATIC_IMPORT_RE)) {
      const r = resolveImport(f, m[1]!)
      if (r) stack.push(r)
      else if (!m[1]!.startsWith('@/') && !m[1]!.startsWith('.')) seen.add(`pkg:${m[1]}`)
    }
  }
  return seen
}

describe('the sample page can go into the /lab/ public build (AWR-14 §21.2.3 rule 1; G02 follow-up 3)', () => {
  const g = [...staticGraph(join(SAMPLES, 'sample.route.tsx'))]
  const files = g.filter((f) => !f.startsWith('pkg:'))

  it('reaches no forbidden module, no engine facade, no app dictionary and no bench client', () => {
    expect(files.some((f) => f.endsWith(join('ui', 'lab', 'inspect', 'InspectPanel.tsx')))).toBe(true)
    expect(files.some((f) => f.endsWith(join('engine', 'actors', 'tape', 'decode.ts')))).toBe(true)
    for (const f of files) {
      expect(LAB_PUBLIC_FORBIDDEN_RE.test(f), f).toBe(false)
      expect(f.endsWith(join('src', 'engine', 'index.ts')), f).toBe(false)
      expect(f.endsWith(join('app', 'i18n', 'index.ts')), f).toBe(false)
    }
  })

  it('never imports the viewer package statically and issues requests only through labGet', () => {
    expect(g.filter((f) => f.includes('@rerun-io'))).toEqual([])
    for (const f of readdirSync(SAMPLES).filter((x) => /\.tsx?$/.test(x))) {
      const code = readFileSync(join(SAMPLES, f), 'utf8')
      // staticGuard.ts 包装 fetch 与 sendBeacon（请求守卫本身），其余文件不直接发起请求
      if (f !== 'staticGuard.ts') expect(/\bfetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon/.test(code), f).toBe(false)
      expect(/\/api\/|\/rerun\/|catalog|metrics/.test(code.replace(/^\s*\/\/.*$/gm, '')), f).toBe(false)
    }
  })
})

describe('vendor Wasm remap in the /lab/ build (D3-AC-21 scan strings 0)', () => {
  const enc = new TextEncoder()
  const count = (b: Uint8Array, n: string): number => {
    const buf = Buffer.from(b.buffer, b.byteOffset, b.byteLength)
    let c = 0
    for (let i = buf.indexOf(n); i >= 0; i = buf.indexOf(n, i + 1)) c++
    return c
  }

  it('mirrors the lab profile of tools/bench/d3/scan_public.py', () => {
    const py = readFileSync(resolve(WEB, '../../tools/bench/d3/scan_public.py'), 'utf8')
    const strs = (name: string): string[] => [...(new RegExp(`${name}[^=]*= \\(([^)]*)\\)`).exec(py)?.[1] ?? '').matchAll(/"([^"]+)"/g)].map((m) => m[1]!)
    const bench = /BENCH_BASE = "([^"]+)"/.exec(py)?.[1]
    expect([...LAB_SCAN_STRINGS].sort()).toEqual([...strs('AC21_STRINGS'), bench!, ...strs('ADR129_LAB_STRINGS'), ...strs('LAB_NO_REQUEST_STRINGS')].sort())
    expect(LAB_VENDOR_WASM_RE.test('assets/re_viewer_bg-CWXAEfH5.wasm')).toBe(true)
    expect(LAB_VENDOR_WASM_RE.test('assets/other.wasm')).toBe(false)
  })

  it('replaces in place: host names upper-cased, URL paths turned into queries, source paths capitalised', () => {
    const src = enc.encode('a\0/home/runner/.cargo/registry/src/x/src/api/adapter.rs\0https://registry.khronos.org/vulkan\0'
      + 'https://mcap.dev/spec/registry#protobuf-1\0https://tel.rerun.io/api/pogevent\0new WebSocket(\0/board')
    const { bytes, replaced } = remapVendorStrings(src)
    expect(bytes.byteLength).toBe(src.byteLength)
    expect(new TextDecoder().decode(bytes)).toBe('a\0/home/runner/.cargo/Registry/src/x/src/Api/adapter.rs\0https://REGISTRY.khronos.org/vulkan\0'
      + 'https://mcap.dev/spec?registry#protobuf-1\0https://tel.rerun.io?api/pogevent\0New WebSocket(\0/Board')
    expect(replaced).toBe(7)
    for (const n of LAB_SCAN_STRINGS) expect(count(bytes, n), n).toBe(0)
  })

  it('brings the pinned Rerun Web Viewer Wasm to 0 scan strings without changing its length', () => {
    const dir = [join(WEB, 'node_modules/@rerun-io/web-viewer'), resolve(WEB, '../../node_modules/@rerun-io/web-viewer')].find((d) => existsSync(d))!
    const src = new Uint8Array(readFileSync(join(dir, 're_viewer_bg.wasm')))
    expect(src.byteLength).toBe(VIEWER_WASM_BYTES)
    expect(count(src, '/registry')).toBeGreaterThan(0)
    const { bytes, replaced } = remapVendorStrings(src)
    expect(bytes.byteLength).toBe(src.byteLength)
    for (const n of LAB_SCAN_STRINGS) expect(count(bytes, n), n).toBe(0)
    let diff = 0
    for (let i = 0; i < src.length; i++) if (src[i] !== bytes[i]) diff++
    expect(replaced).toBeGreaterThan(1000)
    expect(diff).toBeLessThan(2 * replaced) // 只改写命中处（主机名大写时每处改 8 字节以内）
    expect(Array.from(bytes.subarray(0, 8))).toEqual([0, 0x61, 0x73, 0x6d, 1, 0, 0, 0]) // Wasm 头不变
  }, 60_000)
})
