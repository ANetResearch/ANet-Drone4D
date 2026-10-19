// /lab/replay 双运行同步回放（D3-AC-19"两个 Run 同步对比时时间轴一致"；M27-AC-009"同一 t_sim_ns 下观测帧时间戳差 ≤ 1 个帧周期"；
// D3-AC-56"样例包在内部工作区可浏览"）。覆盖：统一时间轴的取帧与同步判定、全程检查与逐毫秒穷举一致、按事件起点对齐、lab 播放头驱动
// 两侧（播放、变速、拖动）、丢帧与无新帧标记、URL 参数、样例包文档校验，以及页面以预置查询缓存的静态渲染（两栏、各机格、同步读数、
// 样例包表格、角色限制）与路由、文案分片登记。Playwright `lab/replay` 的端到端覆盖见 F09 的后续事项。
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createElement as h, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it } from 'vitest'
import { setLocale, t } from '@/app/i18n'
import { route } from '@/app/routes/lab/replay.route'
import { usePlayhead } from '@/stores/lab/playhead'
import { frameIndexKey, ReplayPage, replayRunKey, replayRunsKey, samplesKey } from '@/ui/lab/replay/ReplayPage'
import { artifactHref, bytesText, parseSamplePack, parseSamplePacks, safeArtifactPath, SAMPLE_PACK_SCHEMA } from '@/ui/lab/replay/samplePack'
import {
  alignShifts, assignRun, clockText, commonCams, parseReplaySearch, pickFrame, replayChannels, signedS, syncAt, sweepSync, tracksOf, unifiedSpan,
  type ReplayTrack, type SideTracks,
} from '@/ui/lab/replay/sync'

const WEB = join(import.meta.dirname, '..', '..')
const P = 100_000_000                                                   // rgb1080-rs 10 Hz 帧周期（ns）

/** 确定性伪随机（mulberry32），用于帧时刻抖动 */
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let x = Math.imul(a ^ (a >>> 15), 1 | a)
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296
  }
}

/**
 * 一个通道的帧索引：t_ns = t0 + k·P + 抖动（卷帘读出中点、锁步调度抖动 ±jitter），序号自 0 起；`drop` 中的 k 不出帧（模拟丢帧）
 */
function track(uav: string, n: number, o: { t0?: number; jitter?: number; seed?: number; drop?: readonly number[]; period?: number } = {}): ReplayTrack {
  const period = o.period ?? P
  const r = rng(o.seed ?? 1)
  const items = []
  for (let k = 0; k < n; k++) {
    const j = o.jitter ? Math.round((r() * 2 - 1) * o.jitter) : 0
    if (o.drop?.includes(k)) continue
    items.push({ seq: k, t_ns: (o.t0 ?? 0) + k * period + j })
  }
  return { uav, periodNs: period, items }
}

const UAVS = ['uav-01', 'uav-02', 'uav-03']
/** 360 s 正式运行规模的两侧（3 机 × 3600 帧），B 的首帧晚 37 ms、抖动 ±3 ms，与 A 不同种子 */
const sideOf = (o: Parameters<typeof track>[2] = {}, shiftNs = 0, n = 3600): SideTracks =>
  ({ shiftNs, tracks: UAVS.map((u, i) => track(u, n, { ...o, seed: (o.seed ?? 1) * 10 + i })) })

/** 逐 1 ms 穷举 [lo, hi]：各机两侧所取帧在统一时间轴上的最大 |Δ| */
function bruteMax(a: SideTracks, b: SideTracks, loNs: number, hiNs: number, stepNs = 1_000_000): number {
  let worst = 0
  for (let u = loNs; u <= hiNs; u += stepNs) {
    const s = syncAt(a, b, u)
    if (s.maxAbsDeltaNs !== null) worst = Math.max(worst, s.maxAbsDeltaNs)
  }
  return worst
}

describe('unified timeline (sync.ts)', () => {
  it('picks the last frame at or before the local time of each side, never a future frame', () => {
    const tr = track('uav-01', 50, { t0: 10_000_000, jitter: 2_000_000, seed: 7 })
    for (let u = 0; u <= 5.2e9; u += 7_300_000) {
      for (const shift of [0, 250_000_000, -40_000_000]) {
        const p = pickFrame(tr, u, shift)
        expect(p.tLocalNs).toBe(Math.round(u + shift))
        if (p.item) {
          expect(p.item.t_ns).toBeLessThanOrEqual(p.tLocalNs)
          const next = tr.items[p.i + 1]
          if (next) expect(next.t_ns).toBeGreaterThan(p.tLocalNs)
          expect(p.tUnifiedNs).toBe(p.item.t_ns - shift)
        } else {
          expect(p.i).toBe(-1)
          expect(tr.items[0]!.t_ns).toBeGreaterThan(p.tLocalNs)
        }
      }
    }
    // 超过 1.5 个帧周期标记无新帧（与观测墙相同）
    expect(pickFrame(tr, tr.items.at(-1)!.t_ns + 1.6 * P, 0).stale).toBe(true)
    expect(pickFrame(tr, tr.items.at(-1)!.t_ns + 1.4 * P, 0).stale).toBe(false)
  })

  it('keeps both runs within one frame period at every t_sim_ns of a 360 s, 3-UAV replay (M27-AC-009)', () => {
    // 锁步仿真的帧时刻在 10 Hz 栅格上（相机帧间隔恒为 100 ms，t_mid 含卷帘读出中点 10.5 ms）；B 的起始相位晚 37 ms
    const a = sideOf({ t0: 10_500_000, seed: 1 })
    const b = sideOf({ t0: 47_500_000, seed: 2 })
    const sw = sweepSync(a, b)
    expect(sw.ok).toBe(true)
    expect(sw.pairs.map((p) => p.uav)).toEqual(UAVS)
    expect(sw.maxAbsDeltaNs!).toBeLessThanOrEqual(P)
    // 每机比较点为两侧帧时刻的并集（约 2 × 3600）
    for (const p of sw.pairs) expect(p.points).toBeGreaterThan(7000)
    // 播放头逐 10 ms 走完全程：每个时刻各机都可比较且 |Δ| ≤ 1 个帧周期
    const span = unifiedSpan([a, b])!
    let checked = 0
    for (let u = 0.05e9; u <= span.endS * 1e9; u += 10_000_000) {
      const s = syncAt(a, b, u)
      expect(s.compared).toBe(3)
      expect(s.ok).toBe(true)
      expect(s.maxAbsDeltaNs!).toBeLessThanOrEqual(s.periodNs)
      checked++
    }
    expect(checked).toBeGreaterThan(35_000)
    // 两侧帧率不同（10 Hz 与 30 Hz）时以较大的帧周期为阈值，仍然一致
    const fast = sideOf({ t0: 4_000_000, period: 33_333_333, seed: 3 }, 0, 10_800)
    expect(sweepSync(a, fast)).toMatchObject({ ok: true })
  })

  it('reports per-frame timing jitter honestly: the bound is the actual frame gap, which jitter can push past one period', () => {
    // 同一栅格、逐帧抖动 ±1 ms：两侧帧时刻几乎重合，一侧先出新帧的亚毫秒窗口内 |Δ| 可达 P + 2 ms，全程检查如实报告
    const a = sideOf({ t0: 20_000_000, jitter: 1_000_000, seed: 5 }, 0, 600)
    const b = sideOf({ t0: 20_000_000, jitter: 1_000_000, seed: 6 }, 0, 600)
    const sw = sweepSync(a, b)
    expect(sw.ok).toBe(false)
    expect(sw.maxAbsDeltaNs!).toBeGreaterThan(P)
    expect(sw.maxAbsDeltaNs!).toBeLessThanOrEqual(P + 2_000_000)
  })

  it('computes the exact full-run maximum: the merge sweep equals a 1 ms brute force', () => {
    const a = sideOf({ t0: 3_000_000, jitter: 9_000_000, seed: 3 }, 0, 120)
    const b = sideOf({ t0: 61_000_000, jitter: 9_000_000, seed: 4, drop: [40, 41] }, 0, 120)
    const sw = sweepSync(a, b)
    const lo = Math.max(...[a, b].flatMap((s) => s.tracks.map((t) => t.items[0]!.t_ns)))
    const hi = Math.min(...[a, b].flatMap((s) => s.tracks.map((t) => t.items.at(-1)!.t_ns)))
    const brute = bruteMax(a, b, lo, hi)
    // 穷举步长 1 ms 可能错过亚毫秒的峰值，扫描值不小于穷举值且差距不超过 1 ms
    expect(sw.maxAbsDeltaNs!).toBeGreaterThanOrEqual(brute)
    expect(sw.maxAbsDeltaNs! - brute).toBeLessThanOrEqual(1_000_000)
    // B 丢 2 帧：该处两侧帧差超过 1 个帧周期，全程检查判为不一致并给出发生时刻
    expect(sw.ok).toBe(false)
    const worst = sw.pairs.find((p) => !p.ok)!
    expect(worst.worstUNs).toBeGreaterThanOrEqual(40 * P)
    expect(worst.worstUNs).toBeLessThan(43 * P)
    const at = syncAt(a, b, worst.worstUNs)
    expect(at.ok).toBe(false)
    expect(at.pairs.find((p) => p.uav === worst.uav)!.b!.stale).toBe(true)
  })

  it('aligns by event start when the two runs have different event offsets', () => {
    // 同一事件，B 的运行比 A 晚 2.5 s 进入事件：事件时刻 = 运行时刻 + offset_s
    const offA = 12
    const offB = 9.5
    const al = alignShifts('event', offA, offB)
    expect(al).toEqual({ mode: 'event', a: 0, b: 2_500_000_000 })
    const aTracks = sideOf({ t0: 20_000_000, seed: 5 }, 0, 600).tracks
    const bTracks = sideOf({ t0: 23_000_000, seed: 6 }, 0, 600).tracks
    const a: SideTracks = { shiftNs: al.a, tracks: aTracks }
    const bEvent: SideTracks = { shiftNs: al.b, tracks: bTracks }
    const bSim: SideTracks = { shiftNs: 0, tracks: bTracks }
    expect(sweepSync(a, bEvent).ok).toBe(true)
    expect(sweepSync(a, bSim).ok).toBe(true)
    // 事件对齐时两侧所取帧的事件时刻一致（差 ≤ 1 个帧周期）；同一仿真时刻对齐时事件时刻相差 2.5 s
    const evDelta = (b: SideTracks, u: number) => {
      const s = syncAt(a, b, u).pairs[0]!
      return s.a!.item!.t_ns + offA * 1e9 - (s.b!.item!.t_ns + offB * 1e9)
    }
    for (const u of [5e9, 17.33e9, 42.01e9]) {
      expect(Math.abs(evDelta(bEvent, u))).toBeLessThanOrEqual(P)
      expect(Math.abs(evDelta(bSim, u) - 2.5e9)).toBeLessThanOrEqual(P)
    }
    // 统一时间轴以 A 的仿真时间计；B 在统一时刻 u 读取本地 u + 2.5 s
    expect(syncAt(a, bEvent, 30e9).pairs[0]!.b!.tLocalNs).toBe(32.5e9)
    expect(unifiedSpan([a, bEvent])!.startS).toBeCloseTo(-2.477, 3)
    // 任一侧缺事件偏移时回落为同一仿真时刻
    expect(alignShifts('event', 12, null)).toEqual({ mode: 'sim', a: 0, b: 0 })
    expect(alignShifts('sim', 12, 9.5)).toEqual({ mode: 'sim', a: 0, b: 0 })
  })

  it('drives both runs from the one lab playhead while playing, at every rate and after seeks', () => {
    const a = sideOf({ t0: 10_500_000, seed: 8 }, 0, 300)
    const b = sideOf({ t0: 30_500_000, seed: 9 }, 0, 300)
    const span = unifiedSpan([a, b])!
    usePlayhead.setState({ tS: 0, durationS: 0, playing: false, rate: 1 })
    const ph = usePlayhead.getState()
    ph.setDuration(span.endS)
    ph.seek(span.startS)
    for (const rate of [0.25, 1, 4, 8]) {
      usePlayhead.getState().seek(1)
      usePlayhead.getState().setRate(rate)
      usePlayhead.getState().play()
      let last = -1
      for (let i = 0; i < 120 && usePlayhead.getState().playing; i++) {
        usePlayhead.getState().advance(1 / 60)
        const tS = usePlayhead.getState().tS
        expect(tS).toBeGreaterThanOrEqual(last)
        last = tS
        const s = syncAt(a, b, Math.round(tS * 1e9))
        expect(s.ok).toBe(true)
        for (const p of s.pairs) {
          expect(p.a!.tLocalNs).toBe(p.b!.tLocalNs)                 // 同一 t_sim_ns
          expect(Math.abs(p.deltaNs!)).toBeLessThanOrEqual(P)
        }
      }
      usePlayhead.getState().pause()
    }
    // 拖动到任意时刻（含区间外）后两侧仍取同一时刻；超出末尾时停在末帧
    for (const x of [0, 7.3, 18.05, 29.97, 1e6]) {
      usePlayhead.getState().seek(x)
      const tS = usePlayhead.getState().tS
      expect(tS).toBeLessThanOrEqual(span.endS)
      const s = syncAt(a, b, Math.round(tS * 1e9))
      if (s.compared) expect(s.ok).toBe(true)
    }
    usePlayhead.getState().seek(1e6)
    usePlayhead.getState().play()
    expect(usePlayhead.getState().tS).toBe(0)                          // 末尾处播放从头开始（store 规则）
    usePlayhead.getState().pause()
  })

  it('spans the timeline over every channel of both runs and reports unmatched UAVs as not comparable', () => {
    const a: SideTracks = { shiftNs: 0, tracks: [track('uav-01', 10, { t0: 0 }), track('uav-02', 10, { t0: 5e8 })] }
    const b: SideTracks = { shiftNs: 0, tracks: [track('uav-01', 30, { t0: 2e8 }), track('uav-03', 5)] }
    expect(unifiedSpan([a, b])).toEqual({ startS: 0, endS: 3.1 })
    expect(unifiedSpan([{ shiftNs: 0, tracks: [] }])).toBeNull()
    const s = syncAt(a, b, 4e8)
    expect(s.pairs.map((p) => p.uav)).toEqual(['uav-01', 'uav-02', 'uav-03'])
    expect(s.pairs.map((p) => p.ok)).toEqual([true, null, null])
    expect(s.compared).toBe(1)
    expect(s.pairs[0]!.deltaNs).toBe(4e8 - 4e8)
    // 首帧之前无可比较帧
    expect(syncAt(a, b, 1e8).pairs[0]).toMatchObject({ deltaNs: null, ok: null })
    expect(sweepSync(a, b).pairs.map((p) => p.uav)).toEqual(['uav-01'])
    // 两侧周期不同时以较大者为阈值（10 Hz 对 5 Hz）
    const c: SideTracks = { shiftNs: 0, tracks: [track('uav-01', 20, { period: 200_000_000 })] }
    const sw = sweepSync(a, c)
    expect(sw.pairs[0]!.periodNs).toBe(200_000_000)
    expect(sw.ok).toBe(true)
  })

  it('derives channels and tracks from the run details and frame index queries', () => {
    const fa = [{ uav_id: 'uav-02', sensor_id: 'cam0' }, { uav_id: 'uav-01', sensor_id: 'cam0' }, { uav_id: 'uav-01', sensor_id: 'cam0.depth' }, { uav_id: 'uav-01', sensor_id: 'cam1' }]
    const fb = [{ uav_id: 'uav-01', sensor_id: 'cam0' }, { uav_id: 'uav-01', sensor_id: 'lidar0' }]
    expect(commonCams(fa, fb)).toEqual(['cam0'])
    expect(commonCams(fa, undefined)).toEqual(['cam0', 'cam1'])
    expect(commonCams(undefined, undefined)).toEqual([])
    const ch = replayChannels({ rid: 'run-aaaaaaaaaa', frames: fa }, { rid: 'run-bbbbbbbbbb', frames: fb }, 'cam0')
    expect(ch).toEqual([
      { side: 'a', rid: 'run-aaaaaaaaaa', uav: 'uav-01' }, { side: 'a', rid: 'run-aaaaaaaaaa', uav: 'uav-02' }, { side: 'b', rid: 'run-bbbbbbbbbb', uav: 'uav-01' },
    ])
    expect(replayChannels({ rid: null, frames: fa }, { rid: 'run-bbbbbbbbbb', frames: undefined }, 'cam0')).toEqual([])
    const tr = tracksOf(ch, [{ periodNs: 50_000_000, items: [{ seq: 0, t_ns: 0 }] }, undefined, { periodNs: 0, items: [] }])
    expect(tr.a.map((x) => [x.uav, x.periodNs, x.items.length])).toEqual([['uav-01', 50_000_000, 1], ['uav-02', P, 0]])
    expect(tr.b[0]!.periodNs).toBe(P)
  })

  it('parses the URL and keeps A and B distinct when a run is assigned to the other side', () => {
    const s = parseReplaySearch(new URLSearchParams('a=run-aaaaaaaaaa&b=run-bbbbbbbbbb&t=37.25&cam=cam0&align=event&rate=4'))
    expect(s).toEqual({ a: 'run-aaaaaaaaaa', b: 'run-bbbbbbbbbb', t: 37.25, cam: 'cam0', align: 'event', rate: 4 })
    expect(parseReplaySearch(new URLSearchParams('a=../x&b=%20&t=-1&cam=a/b&align=x&rate=3'))).toEqual({ a: null, b: null, t: null, cam: null, align: 'sim', rate: 1 })
    expect(parseReplaySearch(new URLSearchParams('a=cap-0001')).a).toBe('cap-0001')
    const cur = { a: 'run-aaaaaaaaaa', b: 'run-bbbbbbbbbb' }
    expect(assignRun(cur, 'b', 'run-cccccccccc')).toEqual({ a: 'run-aaaaaaaaaa', b: 'run-cccccccccc' })
    expect(assignRun(cur, 'a', 'run-bbbbbbbbbb')).toEqual({ a: 'run-bbbbbbbbbb', b: 'run-aaaaaaaaaa' })
    expect(assignRun(cur, 'a', null)).toEqual({ a: null, b: 'run-bbbbbbbbbb' })
    expect(clockText(3725.25)).toBe('01:02:05.3')
    expect(clockText(37.2)).toBe('00:00:37.2')
    expect(signedS(-2_500_000_000)).toBe('−2.500')
    expect(signedS(2_500_000_000)).toBe('+2.500')
  })
})

// ── 样例包 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

const sample = (group: string, policy: string, run: string, ec: number) => ({
  group, policy, run_id: run, sim_s: 360, metrics: { ec, eu: 0.41, ao: null, ce: 0.12 },
  files: [
    ...UAVS.map((u) => ({ kind: 'video', path: `video/${u}_cam0.mp4`, bytes: 18_874_368, sha256: 'a'.repeat(64), uav: u, cam: 'cam0' })),
    { kind: 'mcap', path: 'raw.mcap', bytes: 4_200_000_000, sha256: 'b'.repeat(64) },
    { kind: 'metrics', path: 'evaluation.json', bytes: 2048, sha256: null },
  ],
})
const PACK = {
  schema: SAMPLE_PACK_SCHEMA, pack_id: 'mbl-p0a-2026.10.19-1', generated_utc: '2026-10-19T08:30:00Z', event_id: 'crop-handoff-001', event_version: 1,
  world_id: 'oryahovo-crop1', board_version: 'proto-3-l2',
  samples: [
    sample('l2', 'track_share', 'run-cccccccccc', 0.82), sample('l1', 'track_share', 'run-aaaaaaaaaa', 1.0), sample('l1', 'coverage_scan', 'run-bbbbbbbbbb', 0.6),
    { ...sample('l2', 'coverage_scan', 'run-dddddddddd', 0.5), metrics: { ec: 'x' } },
    { ...sample('l2', 'ref_det', '../etc', 0.5) },
  ],
}

describe('sample pack (samplePack.ts)', () => {
  it('validates the pack, orders samples by group and policy and drops invalid records', () => {
    const p = parseSamplePack(PACK)!
    expect(p.packId).toBe('mbl-p0a-2026.10.19-1')
    expect(p.samples.map((s) => `${s.group}/${s.policy}`)).toEqual(['l1/coverage_scan', 'l1/track_share', 'l2/track_share'])
    expect(p.dropped).toBe(2)
    expect(p.samples[0]!.files.map((f) => f.kind)).toEqual(['video', 'video', 'video', 'mcap', 'metrics'])
    expect(p.samples[0]!.metrics).toEqual({ ec: 0.6, eu: 0.41, ao: null, ce: 0.12 })
    expect(JSON.stringify(p)).not.toMatch(/seed/)
    expect(parseSamplePack({ ...PACK, schema: 'drone4d.bench.sample_pack.v0' })).toBeNull()
    expect(parseSamplePack({ ...PACK, event_id: 'Bad Id' })).toBeNull()
    const packs = parseSamplePacks({ items: [{ ...PACK, pack_id: 'older', generated_utc: '2026-10-01T00:00:00Z' }, PACK, null, 3] })
    expect(packs.map((x) => x.packId)).toEqual(['mbl-p0a-2026.10.19-1', 'older'])
    expect(parseSamplePacks(null)).toEqual([])
  })

  it('accepts only relative artifact paths and builds BN-54 download links', () => {
    for (const ok of ['raw.mcap', 'video/uav-01_cam0.mp4', 'slam/uav-01/livo/r1/metrics.json']) expect(safeArtifactPath(ok), ok).toBe(true)
    for (const bad of ['', '/etc/passwd', '../raw.mcap', 'video/../../x', 'a\\b', 'https://x/y', 'javascript:alert(1)', 'a?b=1', 'a#b', 'a//b', 'a/ b', 'x'.repeat(300)]) {
      expect(safeArtifactPath(bad), bad).toBe(false)
    }
    const p = parseSamplePack({ ...PACK, samples: [{ ...sample('l1', 'p', 'run-aaaaaaaaaa', 1), files: [{ kind: 'video', path: '../x.mp4' }, { kind: 'exe', path: 'a.bin' }] }] })!
    expect(p.samples[0]!.files).toEqual([])
    expect(artifactHref('run-aaaaaaaaaa', 'video/uav-01_cam0.mp4')).toBe('/api/bench/v1/runs/run-aaaaaaaaaa/artifacts/video/uav-01_cam0.mp4')
    expect([bytesText(null), bytesText(512), bytesText(18_874_368), bytesText(4_200_000_000)]).toEqual(['—', '512 B', '18.0 MiB', '3.9 GiB'])
  })
})

// ── 页面 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

const RA = 'run-aaaaaaaaaa'
const RB = 'run-bbbbbbbbbb'
const chans = (u: readonly string[]) => [...u.map((x) => ({ uav_id: x, sensor_id: 'cam0', latest_seq: 299 })), { uav_id: 'uav-01', sensor_id: 'cam0.depth', latest_seq: 299 }]
const runDoc = (rid: string, policy: string, off: number) => ({
  run_id: rid, event_id: 'crop-handoff-001', state: 'COMPLETED', kind: 'bench', world_id: 'oryahovo-crop1', policy, has_frames: true,
  frames: chans(UAVS), event: { event_id: 'crop-handoff-001', version: 1, offset_s: off }, fleet: [],
  evaluation: { components: { ec: policy === 'track_share' ? 1 : 0.6, eu: 0.4, ao: 0.3, ce: 0.1 }, ranked_valid: true, invalid_reasons: [] },
})

function client(o: { role?: string; a?: boolean; b?: boolean; packs?: unknown } = {}): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchInterval: false } } })
  qc.setQueryData(['lab', 'session'], { principal: 'alice', role: o.role ?? 'researcher', projects: ['d3'], via: 'cookie' })
  qc.setQueryData(replayRunsKey, { items: [runDoc(RA, 'track_share', 12), runDoc(RB, 'coverage_scan', 12), { ...runDoc('cap-0001', 'x', 0), has_frames: false }], next_cursor: null, total: 3 })
  qc.setQueryData(samplesKey, o.packs ?? parseSamplePacks({ items: [PACK] }))
  qc.setQueryData(replayRunKey(RA), runDoc(RA, 'track_share', 12))
  qc.setQueryData(replayRunKey(RB), runDoc(RB, 'coverage_scan', 9.5))
  UAVS.forEach((u, i) => {
    const a = track(u, 300, { t0: 10_000_000, jitter: 2_000_000, seed: 20 + i })
    const b = track(u, 300, { t0: 40_000_000, jitter: 2_000_000, seed: 30 + i })
    qc.setQueryData(frameIndexKey(RA, u, 'cam0'), { periodNs: P, items: a.items.map((x) => ({ ...x, bytes: 90_000, sha256: 'c'.repeat(64) })) })
    qc.setQueryData(frameIndexKey(RB, u, 'cam0'), { periodNs: P, items: b.items.map((x) => ({ ...x, bytes: 90_000, sha256: 'd'.repeat(64) })) })
  })
  return qc
}
const render = (el: ReactElement, o?: Parameters<typeof client>[0]) => renderToStaticMarkup(h(QueryClientProvider, { client: client(o) }, el))
const page = (q: string) => h(ReplayPage, { params: {}, search: new URLSearchParams(q) })
const attrs = (html: string, name: string) => [...html.matchAll(new RegExp(`${name}="([^"]*)"`, 'g'))].map((m) => m[1]!)

/** 静态渲染读取 zustand 的初始状态（useSyncExternalStore 的服务端快照），因此播放头时刻同时写入当前状态与初始状态 */
const INITIAL = { ...usePlayhead.getInitialState() }
function atPlayhead(tS: number, durationS = 30): void {
  const st = { tS, durationS, playing: false, rate: 1 }
  usePlayhead.setState(st)
  Object.assign(usePlayhead.getInitialState(), st)
}

describe('/lab/replay page', () => {
  afterEach(() => {
    setLocale('zh-CN')
    usePlayhead.setState({ tS: 0, durationS: 0, playing: false, rate: 1 })
    Object.assign(usePlayhead.getInitialState(), INITIAL)
  })

  it('renders both runs with one tile per UAV, all chosen at the same t_sim_ns and within one frame period', () => {
    atPlayhead(12.34)
    const html = render(page(`a=${RA}&b=${RB}`))
    expect(html).toContain('data-view="lab-replay"')
    expect(attrs(html, 'data-replay-pane')).toEqual(['a', 'b'])
    const tiles = attrs(html, 'data-replay-tile')
    expect(tiles).toEqual([...UAVS.map((u) => `a/${u}/cam0`), ...UAVS.map((u) => `b/${u}/cam0`)])
    const ts = attrs(html, 'data-t-ns').map(Number)
    expect(ts.every((x) => x > 12.34e9 - 1.5 * P)).toBe(true)
    for (const x of ts) expect(x).toBeLessThanOrEqual(12.34e9)                     // 不取未来帧
    for (let i = 0; i < 3; i++) expect(Math.abs(ts[i]! - ts[i + 3]!)).toBeLessThanOrEqual(P)
    expect(attrs(html, 'data-sync-ok')).toEqual(['true'])
    expect(Number(attrs(html, 'data-sync-delta-ns')[0])).toBeLessThanOrEqual(P)
    expect(attrs(html, 'data-replay-sweep')).toEqual(['true'])
    expect(attrs(html, 'data-replay-align')).toEqual(['sim'])
    expect(html).toContain('T+00:00:12.3')
    expect(html).toContain(t('lab.replay.title'))
    // 两侧选齐后样例包经侧栏打开，不在页内展开
    expect(html).toContain('data-replay-pack-open')
    expect(html).not.toContain('data-figure="lab-replay-pack"')
    // 运行选择只列有帧数据的运行
    expect(html).not.toContain('>cap-0001')
  })

  it('switches to event-start alignment and shows the offset of run B', () => {
    atPlayhead(5)
    const html = render(page(`a=${RA}&b=${RB}&align=event`))
    expect(attrs(html, 'data-replay-align')).toEqual(['event'])
    expect(attrs(html, 'data-replay-shift')).toEqual(['2500000000'])
    const ts = attrs(html, 'data-t-ns').map(Number)
    for (let i = 0; i < 3; i++) expect(ts[i]!).toBeLessThanOrEqual(5e9)
    for (let i = 3; i < 6; i++) {
      expect(ts[i]!).toBeLessThanOrEqual(7.5e9)
      expect(ts[i]!).toBeGreaterThan(7.5e9 - 1.5 * P)
    }
    expect(html).toContain(t('lab.replay.shift', { s: '+2.500' }))
  })

  it('lists the sample pack inline until both runs are chosen, with metrics, files and A/B actions', () => {
    const html = render(page(`a=${RA}`))
    expect(html).toContain('data-figure="lab-replay-pack"')
    expect(html).toContain('data-pack-meta="mbl-p0a-2026.10.19-1"')
    expect(attrs(html, 'data-pack-assign')).toEqual([
      `a/${RB}`, `b/${RB}`, `a/${RA}`, `b/${RA}`, 'a/run-cccccccccc', 'b/run-cccccccccc',
    ])
    expect(attrs(html, 'data-pack-side')).toEqual(['a'])
    expect(attrs(html, 'data-pack-files')).toEqual([RB, RA, 'run-cccccccccc'])
    expect(html).toContain('1.000')                                           // track_share 的 EC
    expect(html).toContain('data-pack-dropped="2"')
    expect(attrs(html, 'data-replay-pane')).toEqual(['a', 'b'])
    expect(html).toContain(t('lab.replay.pickHint.b'))
    setLocale('en')
    const en = render(page(''))
    expect(en).toContain('Sample pack')
    expect(en).toContain('Set as run A')
  })

  it('shows an empty sample pack state and refuses invited members (frames need the researcher role)', () => {
    const none = render(page(''), { packs: [] })
    expect(none).toContain(t('lab.replay.pack.empty'))
    const inv = render(page(`a=${RA}&b=${RB}`), { role: 'invited' })
    expect(inv).toContain(t('lab.replay.roleDenied'))
    expect(inv).not.toContain('data-replay-tile')
  })

  it('registers the lab-replay route shard and a paired zh-CN/en text shard under lab.replay.*', () => {
    expect(route).toMatchObject({ id: 'lab-replay', pattern: '/lab/replay', layer: 'overlay' })
    expect(route.component).toBe(ReplayPage)
    const load = (l: string) => JSON.parse(readFileSync(join(WEB, `src/app/i18n/lab/replay.${l}.json`), 'utf8')) as Record<string, string>
    const zh = load('zh-CN')
    const e = load('en')
    expect(Object.keys(e).toSorted()).toEqual(Object.keys(zh).toSorted())
    for (const k of Object.keys(zh)) expect(k.startsWith('lab.replay.'), k).toBe(true)
    for (const v of [...Object.values(zh), ...Object.values(e)]) {
      expect(v).not.toMatch(/[!！~～]|2\.0|Drone4D v2|封板/)
      expect(v.trim()).toBe(v)
    }
    expect(zh['lab.replay.docTitle']).toBe('同步回放 · 事件沙盘 · ANet Drone4D')
    expect(e['lab.replay.docTitle']).toBe('Synchronized replay · Event Lab · ANet Drone4D')
    // 页面与组件源码不含中文字面量（I18N-01：文案只在词典中）
    for (const f of ['ReplayPage.tsx', 'ReplayTile.tsx', 'SamplePackPanel.tsx', 'sync.ts', 'samplePack.ts']) {
      const src = readFileSync(join(WEB, 'src/ui/lab/replay', f), 'utf8').split('\n').filter((l) => !/^\s*(\/\/|\/\*\*|\*)/.test(l)).join('\n')
      expect(src.replace(/\/\/.*$/gm, ''), f).not.toMatch(/[一-鿿]/)
    }
  })
})
