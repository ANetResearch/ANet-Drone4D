// /lab/runs/:rid/lidar LiDAR 与重建对比（D3-AC-15"'LiDAR 与重建对比'视图在 Tier B 以裁剪网格正确显示三方叠加，在 Tier S 以点云降级显示；
// 到网格距离着色与 eval_sim 数值一致，RPE、ATE、漂移分布正确"；UX-AC-095；M27-AC-025"到网格距离着色与 eval_sim 抽样 1 万点差 ≤ 1 mm"）。
// 覆盖：误差量化与 eval_sim `mesh_error_u8`（Python）逐字节对拍（1 万个 float32 距离的 sha256）与反量化误差、灰阶色阶端点、ANET_Q16 单节点解码、
// Tier S（D2 点预算阶梯 Tier S 档上限）与 Tier B（每路 2,000,000 点、网格 300,000 面）预算、文档抽样点核对（≤ 2 mm）、箱线图分位数与 NumPy
// 一致、投影点选与框选统计、文档校验与选择、three.js 场景（三方叠加、点形、着色方式、半透明网格、轨迹线型），以及页面以预置查询缓存的
// 静态渲染（Tier B 三方叠加、Tier S 点云降级、指标、箱线图、Jaccard 矩阵、沿程曲线、核对读数、角色与缺失状态）与路由、文案分片登记。
// 真实 GPU 上的帧率与像素级检查属 Playwright `lab/lidar-compare` 与 perf/lab/lidar.spec.ts（见 F06 遗留）。
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createElement as h, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BufferGeometry, Float32BufferAttribute, Group, LineDashedMaterial, Mesh, MeshBasicMaterial, PerspectiveCamera, type Points, type ShaderMaterial, Uint32BufferAttribute } from 'three'
import { afterEach, describe, expect, it } from 'vitest'
import { setLocale, t } from '@/app/i18n'
import { route } from '@/app/routes/lab/lidar-compare.route'
import { LADDER } from '@/engine'
import { BenchError } from '@/net/bench'
import {
  boxStats, budgetStrides, checkErrSamples, COMPARE_DOC, compareArtifactPath, CompareDocError, decodePointLayer, ERR_NONE, errGray, errStats, errU8ToM,
  layersFor, LIDAR_COMPARE_SCHEMA, medianOf, parseCompareDoc, parseCompareSearch, pickNearest, projectPx, quantizeErrU8, quantileSorted, rectErrStats,
  resolveSelection, shownCount, strideFor, TIER_B_MESH_FACES, TIER_B_POINTS_PER_MAP, TIER_S_POINTS_TOTAL, type DecodedPoints, type PointLayerDesc,
} from '@/ui/lab/lidar-compare/compare'
import { compareDocKey, compareLayerKey, LidarComparePage, type LoadedLayer } from '@/ui/lab/lidar-compare/ComparePage'
import { cssRgb, faceCount, MESH_OPACITY, pointsGeometry, pointsObject, styleTruthMesh, trajectoryObject, type SceneTokens } from '@/ui/lab/lidar-compare/scene'

const WEB = join(import.meta.dirname, '..', '..')
const RID = 'run-k2m4q7xz3a'

/** 确定性伪随机（mulberry32，与 Python 参考脚本相同的 32 位运算） */
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let x = Math.imul(a ^ (a >>> 15), 1 | a)
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296
  }
}
/** eval_sim `mesh_distance` 输出为 float32：mulberry32(seed) × 0.7 m 取 float32 */
const distances = (n: number, seed = 15): number[] => {
  const r = rng(seed)
  return Array.from({ length: n }, () => Math.fround(r() * 0.7))
}

/** 主题令牌（theme.css 深色根）：本文件不写颜色字面量，取 --g* 的定义 */
function themeToken(name: string): string {
  const css = readFileSync(join(WEB, 'src/styles/theme.css'), 'utf8')
  const m = new RegExp(`${name}:\\s*([^;]+);`).exec(css)
  return m ? m[1]!.trim() : ''
}
const TK: SceneTokens = {
  lo: cssRgb(themeToken('--g400')), hi: cssRgb(themeToken('--g50')), none: cssRgb(themeToken('--g600')), mesh: cssRgb(themeToken('--g400')),
  gtTraj: cssRgb(themeToken('--g300')), estTraj: cssRgb(themeToken('--g50')), crop: cssRgb(themeToken('--g500')),
}

// ---------------------------------------------------------------------------------------------------- ANET_Q16 编码（测试侧生产者）
interface Pt { x: number; y: number; z: number; r: number; g: number; b: number; e?: number }
/** 单节点 ANET_Q16 载荷：pos u16[4n]、col u8[4n]、bpp 16 时 ext u8[4n]（ext[0] 为误差 u8） */
function encodeQ16(pts: readonly Pt[], cubeMin: readonly [number, number, number], cubeSize: number, bpp: 12 | 16): ArrayBuffer {
  const n = pts.length
  const buf = new ArrayBuffer(n * bpp)
  const q = new Uint16Array(buf, 0, 4 * n)
  const col = new Uint8Array(buf, 8 * n, 4 * n)
  const ext = bpp === 16 ? new Uint8Array(buf, 12 * n, 4 * n) : null
  pts.forEach((p, k) => {
    q[4 * k] = Math.round(((p.x - cubeMin[0]) / cubeSize) * 65535)
    q[4 * k + 1] = Math.round(((p.y - cubeMin[1]) / cubeSize) * 65535)
    q[4 * k + 2] = Math.round(((p.z - cubeMin[2]) / cubeSize) * 65535)
    col.set([p.r, p.g, p.b, 0], 4 * k)
    if (ext) ext[4 * k] = p.e ?? ERR_NONE
  })
  return buf
}
/** 一路地图：n 个点在 [0, 200]² × [0, 30] 内，误差 u8 由 eval_sim 距离量化 */
function mapPoints(n: number, seed: number): { pts: Pt[]; d: number[] } {
  const r = rng(seed)
  const d = distances(n, seed + 1)
  return { pts: d.map((x) => ({ x: r() * 200, y: r() * 200, z: r() * 30, r: 128, g: 120, b: 110, e: quantizeErrU8(x) })), d }
}

// ---------------------------------------------------------------------------------------------------- 文档夹具
const UAVS = ['uav-01', 'uav-02', 'uav-03']
/** uav-01 LIVO 的 15 次 RPE100（%）；NumPy percentile [10, 25, 50, 75, 90] = [0.184, 0.205, 0.24, 0.275, 0.296]（scratchpad f06/ref.py） */
const RPE_LIVO = [0.21, 0.19, 0.25, 0.31, 0.17, 0.22, 0.28, 0.20, 0.24, 0.26, 0.18, 0.23, 0.27, 0.29, 0.30]
const NP_PCT = [0.184, 0.20500000000000002, 0.24, 0.275, 0.296]
const CUBE: [number, number, number] = [-1, -1, -1]
const SIZE = 256

const B_L0 = mapPoints(1500, 100)
const B_LIVO = mapPoints(1200, 200)
const S_GT: Pt[] = Array.from({ length: 900 }, (_, i) => ({ x: (i % 30) * 6, y: Math.floor(i / 30) * 6, z: 0, r: 90, g: 92, b: 96 }))
const S_L0 = mapPoints(800, 300)
const S_LIVO = mapPoints(700, 400)

function layer(src: 'gt' | 'l0' | 'livo', tier: 'B' | 'S', path: string, n: number, extra: Record<string, unknown> = {}) {
  return { kind: 'points', src, tier, path, n, bpp: src === 'gt' ? 12 : 16, cube_min: CUBE, cube_size: SIZE, err: src !== 'gt', ...extra }
}
function runOf(uav: string, mode: 'livo' | 'lio', k: number) {
  const rpe = uav === 'uav-01' && mode === 'livo' ? RPE_LIVO[k]! : mode === 'lio' ? 0.3 + 0.01 * k : 0.2 + 0.005 * k
  const rep = (k % 3) + 1
  return {
    uav, mode, rep, seed: Math.floor(k / 3), ok: true, code: null,
    metrics: { rpe100_pct: rpe, ate_aligned_rmse_m: 0.1 + 0.01 * k, drift_max_m: 1 + 0.1 * k, drift_final_m: 0.8, 'fscore@0.2': 0.8 + 0.005 * k, 'fscore@0.2_aligned': 0.85, color_mae_dn: 11 + k * 0.1, map_to_mesh_mean_m: 0.07 },
    segments: [0, 100, 200, 300].map((s, i) => ({ s_m: s, rpe_pct: rpe + i * 0.01, drift_m: 0.2 * (i + 1), at: [s / 2, 10, 30] })),
    traj: { gt: [[0, 0, 30], [50, 0, 30], [100, 20, 30]], est: [[0, 0, 30], [50, 0.4, 30.1], [100, 20.8, 30.2]] },
  }
}
// 每机每模式 5 种子 × 3 重复；runs 中的 k 决定 rep 与 seed：同一 (uav, mode, rep) 取第一个
const RUNS = UAVS.flatMap((u) => (['livo', 'lio'] as const).flatMap((m) => Array.from({ length: 15 }, (_, k) => runOf(u, m, k))))
const SAMPLES = [
  ...B_L0.d.slice(0, 600).map((d, i) => ({ layer: 'tier_b/l0_uav-01.q16', i, d_m: d })),
  ...B_LIVO.d.slice(0, 400).map((d, i) => ({ layer: 'tier_b/livo_uav-01_livo_r1.q16', i, d_m: d })),
  ...S_L0.d.slice(0, 500).map((d, i) => ({ layer: 'tier_s/l0_uav-01.q16', i, d_m: d })),
  ...S_LIVO.d.slice(0, 500).map((d, i) => ({ layer: 'tier_s/livo_uav-01_livo_r1.q16', i, d_m: d })),
]
const DOC = {
  schema: LIDAR_COMPARE_SCHEMA, run_id: RID, world_id: 'oryahovo-crop1', vehicle: 'p600_mid360_inv', obs: 'realistic', seeds: 5, repeats: 3, main_mode: 'livo',
  origin: [4210.5, -1830.25, 12], crop: { id: 'stage', min: [0, 0, -2], max: [200, 200, 40] },
  eval_sim: { version: 'l0map-1', err_cap_m: 0.5, err_step_m: 0.002 }, uavs: UAVS, modes: ['livo', 'lio'], runs: [...RUNS, { uav: 'uav-09', mode: 'livo', rep: 1 }],
  l0: {
    version: 'l0map-1', uavs: UAVS, coverage: { 'uav-01': 0.292, 'uav-02': 0.325, 'uav-03': 0.263 }, jaccard: { 'uav-01|uav-02': 0.05, 'uav-01|uav-03': 0, 'uav-02|uav-03': 0.048 },
    jaccard_matrix: [[1, 0.05, 0], [0.05, 1, 0.048], [0, 0.048, 1]], jaccard_max: 0.05, jaccard_ok: true, jaccard_threshold: 0.3, merged_coverage: 0.824, merged_over_best: 2.53,
    per_uav: { 'uav-01': { mesh_distance: { n: 1500, mean_m: 0.08, median_m: 0.07, p90_m: 0.162, p95_m: 0.188, max_m: 0.41, within_half_diag: 0.981, half_diag_m: 0.2165 } } },
  },
  layers: [
    { kind: 'mesh', src: 'gt', tier: 'B', path: 'tier_b/truth_stage.glb', faces: 12 },
    layer('l0', 'B', 'tier_b/l0_uav-01.q16', B_L0.pts.length, { uav: 'uav-01' }),
    layer('livo', 'B', 'tier_b/livo_uav-01_livo_r1.q16', B_LIVO.pts.length, { uav: 'uav-01', mode: 'livo', rep: 1 }),
    layer('gt', 'S', 'tier_s/truth_stage.q16', S_GT.length),
    layer('l0', 'S', 'tier_s/l0_uav-01.q16', S_L0.pts.length, { uav: 'uav-01' }),
    layer('livo', 'S', 'tier_s/livo_uav-01_livo_r1.q16', S_LIVO.pts.length, { uav: 'uav-01', mode: 'livo', rep: 1 }),
    { kind: 'points', src: 'livo', tier: 'S', path: '../escape.q16', n: 1, bpp: 16, cube_min: CUBE, cube_size: 1, err: true, uav: 'uav-01', mode: 'livo', rep: 1 },
  ],
  err_samples: SAMPLES,
  ac14: { realistic: { rpe100_median_range_pct: [0.15, 0.40] } },
}
const descOf = (doc: ReturnType<typeof parseCompareDoc>, path: string) => doc.layers.find((l) => l.path === path) as PointLayerDesc

describe('error colouring matches eval_sim (quantization and grey ramp)', () => {
  it('quantizes 10,000 float32 distances byte for byte like eval_sim mesh_error_u8 (Python reference sha256)', () => {
    const d = distances(10_000)
    // Python 侧同一 mulberry32 序列的前 3 个 float32（scratchpad f06/ref.py）
    expect(d.slice(0, 3)).toEqual([0.16648802161216736, 0.2828873097896576, 0.28195658326148987])
    const u = Uint8Array.from(d, quantizeErrU8)
    expect(Array.from(u.slice(0, 8))).toEqual([83, 141, 141, 156, 78, 231, 250, 107])
    expect(createHash('sha256').update(u).digest('hex')).toBe('db24567c675d61ccadb1490450d0cb74325c7b7fd8ae0466b7d50bbdd4f5a23f')
    expect(u.reduce((a, b) => a + b, 0)).toBe(1_603_773)
    expect(u.filter((x) => x === 250).length).toBe(2907)
    // 反量化误差 ≤ 1 mm（M27-AC-025），0.5 m 以上按截断值
    let worst = 0
    d.forEach((x, i) => {
      worst = Math.max(worst, Math.abs(errU8ToM(u[i]!) - Math.min(x, 0.5)))
    })
    expect(worst).toBeLessThanOrEqual(0.001 + 1e-12)
  })

  it('clips and rounds at the boundaries, and the grey ramp runs from g400 at 0 m to g50 at 0.5 m', () => {
    expect([-0.1, 0, 0.000999, 0.001, 0.002, 0.4999, 0.5, 0.75].map(quantizeErrU8)).toEqual([0, 0, 0, 1, 1, 250, 250, 250])
    expect(errU8ToM(ERR_NONE)).toBeNaN()
    expect(errU8ToM(125)).toBeCloseTo(0.25, 12)
    expect(errGray(0, TK.lo, TK.hi)).toEqual(TK.lo)
    expect(errGray(250, TK.lo, TK.hi)).toEqual(TK.hi)
    const mid = errGray(125, TK.lo, TK.hi)
    for (let c = 0; c < 3; c++) expect(mid[c]).toBeCloseTo((TK.lo[c]! + TK.hi[c]!) / 2, 12)
    // 灰阶：三个分量相同或近似相同（无色相），且 0.5 m 一端更亮
    for (const v of [TK.lo, TK.hi]) expect(Math.max(...v) - Math.min(...v)).toBeLessThan(0.08)
    expect(TK.hi[0]).toBeGreaterThan(TK.lo[0])
  })
})

describe('artifacts, budgets and statistics (compare.ts)', () => {
  const doc = parseCompareDoc(DOC)

  it('decodes ANET_Q16 single-node payloads into local positions, colours and error bytes', () => {
    const L = decodePointLayer(encodeQ16(B_L0.pts, CUBE, SIZE, 16), descOf(doc, 'tier_b/l0_uav-01.q16'))
    expect(L.n).toBe(1500)
    const tol = SIZE / 65535 / 2 + 1e-4
    B_L0.pts.forEach((p, k) => {
      expect(Math.abs(L.pos[3 * k]! - p.x)).toBeLessThanOrEqual(tol)
      expect(Math.abs(L.pos[3 * k + 2]! - p.z)).toBeLessThanOrEqual(tol)
      expect(L.err![k]).toBe(p.e)
    })
    expect(Array.from(L.rgb.slice(0, 3))).toEqual([128, 120, 110])
    const gt = decodePointLayer(encodeQ16(S_GT, CUBE, SIZE, 12), descOf(doc, 'tier_s/truth_stage.q16'))
    expect(gt.err).toBeNull()
    expect(() => decodePointLayer(new ArrayBuffer(16 * 1499), descOf(doc, 'tier_b/l0_uav-01.q16'))).toThrow(CompareDocError)
  })

  it('keeps Tier S under the Tier S ceiling of the D2 point budget ladder and Tier B under 2,000,000 points per map', () => {
    expect(TIER_S_POINTS_TOTAL).toBe(LADDER[1]!.hi)
    expect(TIER_S_POINTS_TOTAL).toBe(150_000)
    expect([TIER_B_POINTS_PER_MAP, TIER_B_MESH_FACES]).toEqual([2_000_000, 300_000])
    expect(budgetStrides([900, 800, 700], 'S')).toEqual([1, 1, 1])
    for (const ns of [[100_000, 100_000, 100_000], [450_000, 1_200_000, 37], [2_000_001], [149_999, 2]]) {
      const st = budgetStrides(ns, 'S')
      const total = ns.reduce((a, n, i) => a + shownCount(n, st[i]!), 0)
      expect(total, ns.join(',')).toBeLessThanOrEqual(TIER_S_POINTS_TOTAL)
    }
    expect(budgetStrides([2_000_000, 2_500_000, 6_100_000], 'B')).toEqual([1, 2, 4])
    expect(shownCount(6_100_000, strideFor(6_100_000, TIER_B_POINTS_PER_MAP))).toBeLessThanOrEqual(TIER_B_POINTS_PER_MAP)
  })

  it('checks the document error samples against the delivered bytes (UX-AC-095: <= 2 mm) and flags a tampered byte', () => {
    const layers = new Map<string, DecodedPoints>([
      ['tier_b/l0_uav-01.q16', decodePointLayer(encodeQ16(B_L0.pts, CUBE, SIZE, 16), descOf(doc, 'tier_b/l0_uav-01.q16'))],
      ['tier_b/livo_uav-01_livo_r1.q16', decodePointLayer(encodeQ16(B_LIVO.pts, CUBE, SIZE, 16), descOf(doc, 'tier_b/livo_uav-01_livo_r1.q16'))],
    ])
    const c = checkErrSamples(doc.errSamples.filter((s) => layers.has(s.layer)), layers)
    expect(c.n).toBe(1000)
    expect(c.missing).toBe(0)
    expect(c.maxAbsDiffM!).toBeLessThanOrEqual(0.001 + 1e-9)
    expect(c.ok).toBe(true)
    const L = layers.get('tier_b/l0_uav-01.q16')!
    L.err![17] = Math.min(250, L.err![17]! + 2)                             // 4 mm 偏差
    const bad = checkErrSamples(doc.errSamples.filter((s) => layers.has(s.layer)), layers)
    expect(bad.ok).toBe(false)
    expect(bad.maxAbsDiffM!).toBeGreaterThan(0.002)
  })

  it('computes box-plot quantiles exactly like NumPy percentile (slam_eval distribution)', () => {
    const b = boxStats(RPE_LIVO)!
    expect([b.p10, b.q1, b.median, b.q3, b.p90]).toEqual(NP_PCT.map((v) => expect.closeTo(v, 12)))
    expect([b.min, b.max, b.n]).toEqual([0.17, 0.31, 15])
    expect(boxStats([null, Number.NaN])).toBeNull()
    expect(quantileSorted([1], 0.9)).toBe(1)
    expect(medianOf(doc, 'uav-01', 'livo', 'rpe100_pct')).toBeCloseTo(0.24, 12)
    // 文档中的 eval_sim 统计（中位数）与本页由运行值计算的中位数一致
    expect(medianOf(doc, 'uav-01', 'livo', 'ate_aligned_rmse_m')).toBeCloseTo(0.17, 12)
    const s = errStats([0, 250, ERR_NONE, 100, 50])
    expect([s.n, s.none]).toEqual([4, 1])
    expect(s.medianM).toBeCloseTo(0.15, 12)
    expect(s.maxM).toBeCloseTo(0.5, 12)
  })

  it('projects, picks the nearest displayed point and aggregates errors inside a screen rectangle', () => {
    const cam = new PerspectiveCamera(50, 2, 0.1, 1000)
    cam.up.set(0, 0, 1)
    cam.position.set(0, -50, 0)
    cam.lookAt(0, 0, 0)
    cam.updateMatrixWorld()
    const m = cam.projectionMatrix.clone().multiply(cam.matrixWorldInverse).elements
    const o = new Float64Array(3)
    expect(projectPx(m, 0, 0, 0, 800, 400, o)).toBe(true)
    expect([o[0], o[1]]).toEqual([expect.closeTo(400, 6), expect.closeTo(200, 6)])
    expect(projectPx(m, 0, -80, 0, 800, 400, o)).toBe(false)                 // 相机后方
    const pts = [{ x: 0, y: 0, z: 0 }, { x: 0.5, y: 0, z: 0 }, { x: -10, y: 0, z: 5 }, { x: 0, y: 10, z: 0 }]
    const desc = descOf(parseCompareDoc(DOC), 'tier_b/l0_uav-01.q16')
    const L: DecodedPoints = {
      desc: { ...desc, n: pts.length }, n: pts.length, pos: Float32Array.from(pts.flatMap((p) => [p.x, p.y, p.z])), rgb: new Uint8Array(12),
      err: Uint8Array.from([10, 20, 30, ERR_NONE]), stride: 1,
    }
    const hit = pickNearest([L], m, 800, 400, 401, 200, 6)!
    expect(hit.i).toBe(0)
    expect(pickNearest([L], m, 800, 400, 700, 50, 6)).toBeNull()
    const all = rectErrStats([L], m, 800, 400, { x0: 0, y0: 0, x1: 800, y1: 400 })
    expect([all.points, all.n, all.none]).toEqual([4, 3, 1])
    const near = rectErrStats([L], m, 800, 400, { x0: 380, y0: 190, x1: 420, y1: 210 })
    expect(near.points).toBe(3)                                               // 原点、0.5 m 处与 y=10 处的点（同一视线附近）
    expect(rectErrStats([{ ...L, stride: 2 }], m, 800, 400, { x0: 0, y0: 0, x1: 800, y1: 400 }).points).toBe(2)
  })

  it('validates the document, drops bad entries and refuses a different quantization', () => {
    expect(doc.uavs).toEqual(UAVS)
    expect(doc.runs.length).toBe(90)
    expect(doc.dropped).toBe(2)                                               // 未登记机号的运行与越界路径的层
    expect(doc.layers.map((l) => l.path)).not.toContain('../escape.q16')
    expect(doc.l0!.matrix[1]![2]).toBe(0.048)
    expect(doc.rpeBand).toEqual([0.15, 0.4])
    expect(() => parseCompareDoc({ ...DOC, schema: 'x' })).toThrow(CompareDocError)
    expect(() => parseCompareDoc({ ...DOC, eval_sim: { err_cap_m: 1, err_step_m: 0.004 } })).toThrow(CompareDocError)
    expect(compareArtifactPath(RID, COMPARE_DOC)).toBe(`/runs/${RID}/artifacts/compare/metrics.json`)
  })

  it('resolves the selection from the URL and picks the three-way layers per tier', () => {
    const q = parseCompareSearch(new URLSearchParams('uav=uav-01&rep=1&mode=livo'))
    const sel = resolveSelection(doc, q)
    expect([sel.uav, sel.mode, sel.rep, sel.reps]).toEqual(['uav-01', 'livo', 1, [1, 2, 3]])
    expect(layersFor(doc, sel, 'B', q.src).map((l) => `${l.src}:${l.kind}`)).toEqual(['gt:mesh', 'l0:points', 'livo:points'])
    expect(layersFor(doc, sel, 'S', q.src).map((l) => `${l.src}:${l.kind}`)).toEqual(['gt:points', 'l0:points', 'livo:points'])
    expect(layersFor(doc, sel, 'S', ['l0']).map((l) => l.src)).toEqual(['l0'])
    const bad = resolveSelection(doc, parseCompareSearch(new URLSearchParams('uav=uav-77&mode=x&rep=9')))
    expect([bad.uav, bad.mode, bad.rep]).toEqual(['uav-01', 'livo', 1])
    expect(parseCompareSearch(new URLSearchParams('src=none')).src).toEqual([])
    expect(parseCompareSearch(new URLSearchParams('src=livo,gt&tier=S&color=rgb&size=4&crop=0'))).toMatchObject({ src: ['gt', 'livo'], tier: 'S', color: 'rgb', size: 4, crop: false })
    expect(parseCompareSearch(new URLSearchParams('size=5&tier=A'))).toMatchObject({ size: 2, tier: null, color: 'err', crop: true })
  })
})

describe('three.js scene (scene.ts)', () => {
  const doc = parseCompareDoc(DOC)
  const l0 = decodePointLayer(encodeQ16(S_L0.pts, CUBE, SIZE, 16), descOf(doc, 'tier_s/l0_uav-01.q16'))
  const livo = decodePointLayer(encodeQ16(S_LIVO.pts, CUBE, SIZE, 16), descOf(doc, 'tier_s/livo_uav-01_livo_r1.q16'))
  const gt = decodePointLayer(encodeQ16(S_GT, CUBE, SIZE, 12), descOf(doc, 'tier_s/truth_stage.q16'))
  const opts = { colorBy: 'err' as const, sizePx: 2, pixelRatio: 0.5 }
  const u = (p: Points, k: string) => (p.material as ShaderMaterial).uniforms[k]!.value as number

  it('draws L0 as solid circles and FAST-LIVO2 as hollow squares coloured by error, the Tier S truth as true-colour squares', () => {
    const [a, b, c] = [pointsObject(l0, TK, opts), pointsObject(livo, TK, opts), pointsObject(gt, TK, opts)]
    expect([u(a, 'uShape'), u(b, 'uShape'), u(c, 'uShape')]).toEqual([0, 1, 2])
    expect([u(a, 'uColorBy'), u(b, 'uColorBy'), u(c, 'uColorBy')]).toEqual([0, 0, 1])
    expect(u(b, 'uSize')).toBe(6)                                             // 空心方点至少 3 个绘制缓冲像素（Tier S 像素比 0.5）
    expect(u(pointsObject(livo, TK, { ...opts, pixelRatio: 2 }), 'uSize')).toBe(2)
    expect(u(a, 'uSize')).toBe(2)
    expect(u(a, 'uPx')).toBe(0.5)
    expect(Array.from(a.geometry.getAttribute('erru8').array as Uint8Array)).toEqual(Array.from(l0.err!))
    expect(Array.from(c.geometry.getAttribute('erru8').array as Uint8Array).every((x) => x === ERR_NONE)).toBe(true)
    expect(pointsObject(l0, TK, { ...opts, colorBy: 'rgb' }).material).toMatchObject({ uniforms: { uColorBy: { value: 1 } } })
    const thin = pointsGeometry({ ...l0, stride: 3 })
    expect(thin.getAttribute('position').count).toBe(Math.ceil(800 / 3))
    expect((thin.getAttribute('erru8').array as Uint8Array)[1]).toBe(l0.err![3])
  })

  it('renders the Tier B truth as a translucent g400 mesh and the trajectories as solid and dashed lines', () => {
    const g = new BufferGeometry()
    g.setAttribute('position', new Float32BufferAttribute(new Float32Array(3 * 6), 3))
    g.setIndex(new Uint32BufferAttribute(Uint32Array.from([0, 1, 2, 3, 4, 5, 0, 2, 4]), 1))
    const root = new Group().add(new Mesh(g), new Mesh(g.clone().setIndex(null)))
    expect(faceCount(root)).toBe(5)
    styleTruthMesh(root, TK)
    const mat = (root.children[0] as Mesh).material as MeshBasicMaterial
    expect(mat).toBeInstanceOf(MeshBasicMaterial)
    expect([mat.transparent, mat.opacity, mat.depthWrite, MESH_OPACITY]).toEqual([true, 0.25, false, 0.25])
    const est = trajectoryObject(Float64Array.from([0, 0, 0, 10, 0, 0, 10, 10, 0]), 'est', TK)
    expect(est.material).toBeInstanceOf(LineDashedMaterial)
    expect(est.geometry.getAttribute('lineDistance').count).toBe(3)
    expect(trajectoryObject(Float64Array.from([0, 0, 0, 1, 1, 1]), 'gt', TK).material).not.toBeInstanceOf(LineDashedMaterial)
  })
})

// ---------------------------------------------------------------------------------------------------- 页面
function meshLayer(): LoadedLayer {
  const g = new BufferGeometry()
  g.setAttribute('position', new Float32BufferAttribute(new Float32Array(36), 3))
  return { kind: 'mesh', root: new Group().add(new Mesh(g)), faces: 4 }
}
function client(o: { role?: string; doc?: unknown; docError?: Error } = {}): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchInterval: false } } })
  qc.setQueryData(['lab', 'session'], { principal: 'alice', role: o.role ?? 'researcher', projects: ['d3'], via: 'cookie' })
  if (o.docError) {
    // 预置失败的文档查询（页面的文档查询不在挂载时重试）
    qc.getQueryCache().build(qc, { queryKey: compareDocKey(RID) }).setState({ status: 'error', error: o.docError, fetchStatus: 'idle', errorUpdateCount: 1 })
    return qc
  }
  const doc = parseCompareDoc(o.doc ?? DOC)
  qc.setQueryData(compareDocKey(RID), doc)
  const put = (path: string, pts: Pt[], bpp: 12 | 16) => qc.setQueryData(compareLayerKey(RID, path), { kind: 'points', data: decodePointLayer(encodeQ16(pts, CUBE, SIZE, bpp), descOf(doc, path)) })
  qc.setQueryData(compareLayerKey(RID, 'tier_b/truth_stage.glb'), meshLayer())
  put('tier_b/l0_uav-01.q16', B_L0.pts, 16)
  put('tier_b/livo_uav-01_livo_r1.q16', B_LIVO.pts, 16)
  put('tier_s/truth_stage.q16', S_GT, 12)
  put('tier_s/l0_uav-01.q16', S_L0.pts, 16)
  put('tier_s/livo_uav-01_livo_r1.q16', S_LIVO.pts, 16)
  return qc
}
const render = (el: ReactElement, o?: Parameters<typeof client>[0]) => renderToStaticMarkup(h(QueryClientProvider, { client: client(o) }, el))
const page = (q: string) => h(LidarComparePage, { params: { rid: RID }, search: new URLSearchParams(q) })
const attrs = (html: string, name: string) => [...html.matchAll(new RegExp(`${name}="([^"]*)"`, 'g'))].map((m) => m[1]!)

describe('/lab/runs/:rid/lidar page', () => {
  afterEach(() => setLocale('zh-CN'))

  it('overlays the cropped truth mesh, the L0 map and the FAST-LIVO2 map on Tier B, with the colour check against eval_sim', () => {
    const html = render(page('tier=B&uav=uav-01&mode=livo&rep=1'))
    expect(html).toContain('data-view="lab-lidar-compare"')
    expect(attrs(html, 'data-compare-canvas')).toEqual(['B'])
    expect(attrs(html, 'data-compare-tier')).toEqual(['B'])
    expect(attrs(html, 'data-compare-layers')).toEqual(['gt:mesh,l0:points,livo:points'])
    expect(attrs(html, 'data-compare-shown')).toEqual([String(1500 + 1200)])
    expect(attrs(html, 'data-compare-thinned')).toEqual(['false'])
    expect(attrs(html, 'data-err-check-n')).toEqual(['1000'])
    expect(Number(attrs(html, 'data-err-check-max-m')[0])).toBeLessThanOrEqual(0.001 + 1e-9)
    expect(attrs(html, 'data-err-check-ok')).toEqual(['true'])
    expect(html).toContain(t('lab.lidarCompare.budget.b', { n: '2,700', max: '2,000,000', faces: '4', fmax: '300,000' }))
    expect(html).toContain('p600_mid360_inv · oryahovo-crop1 · LIVO')
    expect(html).toContain(t('lab.lidarCompare.title'))
  })

  it('falls back to the mesh-sampled point cloud on Tier S within the ladder budget', () => {
    const html = render(page('tier=S'))
    expect(attrs(html, 'data-compare-canvas')).toEqual(['S'])
    expect(attrs(html, 'data-compare-layers')).toEqual(['gt:points,l0:points,livo:points'])
    const shown = Number(attrs(html, 'data-compare-shown')[0])
    expect(shown).toBe(900 + 800 + 700)
    expect(shown).toBeLessThanOrEqual(TIER_S_POINTS_TOTAL)
    expect(attrs(html, 'data-err-check-n')).toEqual(['1000'])
    expect(attrs(html, 'data-err-check-ok')).toEqual(['true'])
    expect(html).toContain(t('lab.lidarCompare.tier.S'))
    expect(html).not.toContain('gt:mesh')
    // 关闭真值后只剩两路地图
    expect(attrs(render(page('tier=S&src=l0,livo')), 'data-compare-layers')).toEqual(['l0:points,livo:points'])
  })

  it('shows the medians, the RPE100 box plot quantiles, the L0 coverage, the Jaccard matrix and the along-track segments', () => {
    const html = render(page('tier=B&uav=uav-01'))
    expect(attrs(html, 'data-compare-stats')).toEqual(['uav-01/livo'])
    expect(html).toContain(t('lab.lidarCompare.stats.cond', { mode: 'LIVO', obs: 'realistic', seeds: 5, repeats: 3, n: 15 }))
    expect(html).toContain('0.24')                                            // RPE100 中位数
    const q = attrs(html, 'data-box-q')
    expect(attrs(html, 'data-box-group')).toEqual(['livo', 'lio'])
    expect(q[0]!.split(',').map(Number)).toEqual(NP_PCT.map((v) => expect.closeTo(v, 10)))
    expect(attrs(html, 'data-lf-band')).toEqual(['0.15,0.4'])
    expect(attrs(html, 'data-jaccard')).toEqual(['', '0.0500', '0.0000', '0.0500', '', '0.0480', '0.0000', '0.0480', ''])
    expect(attrs(html, 'data-jaccard-ok')).toEqual(['true'])
    expect(html).toContain('82.4')
    expect(attrs(html, 'data-compare-evalsim')).toEqual(['uav-01'])
    expect(html).toContain('0.070 m')
    expect(attrs(html, 'data-compare-curve')).toEqual(['4'])
    expect(attrs(html, 'data-segment')).toEqual(['0', '1', '2', '3', '0', '1', '2', '3'])
  })

  it('switches mode and repeat from the URL, and reports layers missing from the artifacts', () => {
    const html = render(page('tier=B&mode=lio&rep=2'))
    expect(attrs(html, 'data-compare-mode')).toEqual(['lio'])
    expect(attrs(html, 'data-compare-rep')).toEqual(['2'])
    expect(attrs(html, 'data-compare-layers')).toEqual(['gt:mesh,l0:points'])
    expect(attrs(html, 'data-compare-note')).toContain('absent-livo')
  })

  it('handles roles, a missing artifact set, a malformed document and English copy', () => {
    const inv = render(page('tier=B'), { role: 'invited' })
    expect(inv).toContain(t('lab.lidarCompare.roleDenied'))
    expect(inv).not.toContain('data-compare-canvas')
    expect(render(page('tier=B'), { docError: new BenchError(404, 305, 'NOT_FOUND', 'missing') })).toContain(t('lab.lidarCompare.missing'))
    expect(render(page('tier=B'), { docError: new CompareDocError('schema') })).toContain(t('lab.lidarCompare.invalid'))
    expect(render(page('tier=B'), { docError: new BenchError(502, null, null, 'bad gateway') })).toContain(t('lab.lidarCompare.unavailable'))
    setLocale('en')
    const en = render(page('tier=S'))
    expect(en).toContain('LiDAR and reconstruction comparison')
    expect(en).toContain('L0 known-pose map')
    expect(en).toContain('Tier S · point cloud fallback')
  })

  it('registers the lab-lidar-compare route shard and a paired zh-CN/en text shard under lab.lidarCompare.*', () => {
    expect(route).toMatchObject({ id: 'lab-lidar-compare', pattern: '/lab/runs/:rid/lidar', layer: 'overlay' })
    expect(route.component).toBe(LidarComparePage)
    expect(route.validate!({ rid: RID })).toBe(true)
    expect(route.validate!({ rid: '../x' })).toBe(false)
    const load = (l: string) => JSON.parse(readFileSync(join(WEB, `src/app/i18n/lab/lidar-compare.${l}.json`), 'utf8')) as Record<string, string>
    const zh = load('zh-CN')
    const e = load('en')
    expect(Object.keys(e).toSorted()).toEqual(Object.keys(zh).toSorted())
    for (const k of Object.keys(zh)) expect(k.startsWith('lab.lidarCompare.'), k).toBe(true)
    for (const v of [...Object.values(zh), ...Object.values(e)]) {
      expect(v).not.toMatch(/[!！~～]|2\.0|Drone4D v2|封板/)
      expect(v.trim()).toBe(v)
    }
    expect(zh['lab.lidarCompare.docTitle']).toBe('LiDAR 与重建对比 · 事件沙盘 · ANet Drone4D')
    expect(e['lab.lidarCompare.docTitle']).toBe('LiDAR and reconstruction comparison · Event Lab · ANet Drone4D')
    // 页面与组件源码不含中文字面量（I18N-01：文案只在词典中）
    for (const f of ['ComparePage.tsx', 'CompareCanvas.tsx', 'charts.tsx', 'compare.ts', 'scene.ts']) {
      const src = readFileSync(join(WEB, 'src/ui/lab/lidar-compare', f), 'utf8').split('\n').filter((l) => !/^\s*(\/\/|\/\*\*|\*)/.test(l)).join('\n')
      expect(src.replace(/\/\/.*$/gm, ''), f).not.toMatch(/[一-鿿]/)
    }
  })
})
