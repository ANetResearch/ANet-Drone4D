// LiDAR 与重建对比（`/lab/runs/:rid/lidar`，D3-AC-15；AWR-05 §7.5；AWR-14 §21.8；M27-FR-109）的数据形状、校验与纯函数。
// 数据来自 M27 派生物 `derived/compare/`：`metrics.json`（文档 `drone4d.bench.lidar_compare.v1`）与其引用的 `tier_b/`、`tier_s/` 制品，
// 内部经 BN-54 `GET /runs/{rid}/artifacts/compare/{path}`。页面不在浏览器中计算到网格距离：每点误差由 `eval_sim` 预计算并量化为 u8
// （`awr.bench.eval.lidar_map.mesh_error_u8`：0–0.5 m、步长 2 mm、四舍五入、超出按 0.5 m，u8 0–250），随点下发；本文件只做反量化、
// 灰阶映射、预算核对与框选统计。`quantizeErrU8` 是同一量化的 TypeScript 镜像，只用于与 `eval_sim` 逐字节对拍及核对文档自带的抽样点。
//
// 文档形状（坐标一律为局部 ENU，米，相对 `origin`；z 向上）：
//   {schema, run_id, world_id, vehicle, obs, seeds, repeats, main_mode, origin[3], crop{id, min[3], max[3]},
//    eval_sim{version, err_cap_m, err_step_m}, uavs[], modes[],
//    runs[]{uav, mode, rep, seed, ok, code, metrics{rpe100_pct, rpe100_median_pct, ate_aligned_rmse_m, drift_max_m, drift_final_m,
//           fscore@0.2, fscore@0.2_aligned, color_mae_dn, map_to_mesh_mean_m}, segments[]{s_m, rpe_pct, drift_m, at[3]},
//           traj{gt[[x,y,z]…], est[[x,y,z]…]}},
//    l0{…`fleet_report` 原样：uavs, coverage, jaccard_matrix, jaccard_max, jaccard_ok, jaccard_threshold, merged_coverage, per_uav{u:{mesh_distance}}},
//    layers[]{src ∈ gt|l0|livo, tier ∈ B|S, kind ∈ points|mesh, uav?, mode?, rep?, path, n | faces, bpp, cube_min[3], cube_size, err, sha256?},
//    err_samples[]{layer, i, d_m}, ac14{realistic{rpe100_median_range_pct[2]}}}
// 点层为 ANET_Q16 v1 单节点载荷（AWR-16 §4.3，不压缩）：pos u16[4n]（qx, qy, qz, oct16）、col u8[4n]（sRGB、类别）、bpp 16 时 ext u8[4n]
// （ext[0] 为误差 u8，255 表示无值）；p = cube_min + q / 65535 · cube_size。网格层为裁剪后的碰撞网格 GLB（顶点为同一局部 ENU，不做 Y 向上换轴）。
import { deviceParams, LADDER, type Tier } from '@/engine'
import { safeArtifactPath } from '@/ui/lab/replay/samplePack'

export const LIDAR_COMPARE_SCHEMA = 'drone4d.bench.lidar_compare.v1'
export const SOURCES = ['gt', 'l0', 'livo'] as const
export type CompareSource = (typeof SOURCES)[number]
export const MODES = ['livo', 'lio'] as const
export type SlamMode = (typeof MODES)[number]
export type CompareTier = 'B' | 'S'
export const COLOR_BY = ['err', 'rgb'] as const
export type ColorBy = (typeof COLOR_BY)[number]

/** 误差量化口径（与 eval_sim `ERR_CAP_M, ERR_STEP_M` 相同；D3-AC-15"到网格距离着色与 eval_sim 数值一致"） */
export const ERR_CAP_M = 0.5
export const ERR_STEP_M = 0.002
export const ERR_U8_MAX = 250
/** ext[0] 的无值标记（点不在网格查询范围内或该层不着色） */
export const ERR_NONE = 255
/** 文档抽样核对的阈值（UX-AC-095：抽样 1,000 点，量化误差 ≤ 2 mm） */
export const ERR_CHECK_TOL_M = 0.002

/**
 * 点与面预算（AWR-14 §21.8.2、§23 LiDAR 对比点预算）：Tier B/A 每路点云 ≤ 2,000,000 点、裁剪网格 ≤ 300,000 面；Tier S 三路合计不超过
 * D2 点预算阶梯的 Tier S 档上限，即软件光栅设备的最高档（`deviceParams` 的 ceilIndex，ADR-012 阶梯）的 hi。
 */
export const TIER_B_POINTS_PER_MAP = 2_000_000
export const TIER_B_MESH_FACES = 300_000
export const TIER_S_POINTS_TOTAL = LADDER[deviceParams('S', 'software', 0, 0).ceilIndex]!.hi

export const METRIC_KEYS = [
  'rpe100_pct', 'rpe100_median_pct', 'ate_aligned_rmse_m', 'drift_max_m', 'drift_final_m', 'fscore@0.2', 'fscore@0.2_aligned', 'color_mae_dn', 'map_to_mesh_mean_m',
] as const
export type MetricKey = (typeof METRIC_KEYS)[number]
/** 指标面板的五项主指标（§21.8.3），F@0.2 锚定与对齐合为一项 */
export const MAIN_STATS = ['rpe100_pct', 'ate_aligned_rmse_m', 'drift_max_m', 'fscore@0.2', 'color_mae_dn'] as const

export type Vec3 = readonly [number, number, number]
export interface Segment { sM: number; rpePct: number | null; driftM: number | null; at: Vec3 | null }
export interface CompareRun {
  uav: string
  mode: SlamMode
  rep: number
  seed: number | null
  ok: boolean
  code: number | null
  metrics: Record<MetricKey, number | null>
  segments: Segment[]
  /** 局部 ENU 折线（xyz 交错，float64） */
  trajGt: Float64Array | null
  trajEst: Float64Array | null
}
export interface PointLayerDesc {
  kind: 'points'
  src: CompareSource
  tier: CompareTier
  uav: string | null
  mode: SlamMode | null
  rep: number | null
  path: string
  n: number
  bpp: 12 | 16
  cubeMin: Vec3
  cubeSize: number
  err: boolean
  sha256: string | null
}
export interface MeshLayerDesc { kind: 'mesh'; src: 'gt'; tier: 'B'; uav: null; mode: null; rep: null; path: string; faces: number; sha256: string | null }
export type LayerDesc = PointLayerDesc | MeshLayerDesc
export interface DistStats { n: number; meanM: number | null; medianM: number | null; p90M: number | null; p95M: number | null; maxM: number | null; withinHalfDiag: number | null; halfDiagM: number | null }
export interface FleetL0 {
  uavs: string[]
  coverage: Record<string, number>
  matrix: number[][]
  jaccardMax: number | null
  jaccardOk: boolean | null
  threshold: number
  merged: number | null
  mergedOverBest: number | null
  meshDistance: Record<string, DistStats>
}
export interface ErrSample { layer: string; i: number; dM: number }
export interface CompareDoc {
  runId: string
  worldId: string | null
  vehicle: string | null
  obs: string | null
  seeds: number | null
  repeats: number | null
  mainMode: SlamMode
  origin: Vec3
  crop: { id: string; min: Vec3; max: Vec3 } | null
  evalVersion: string | null
  uavs: string[]
  modes: SlamMode[]
  runs: CompareRun[]
  l0: FleetL0 | null
  layers: LayerDesc[]
  errSamples: ErrSample[]
  /** D3-AC-14 realistic 档 RPE100 中位数区间（%），箱线图参考带 */
  rpeBand: readonly [number, number] | null
  /** 校验时丢弃的条目数（运行、层、抽样点） */
  dropped: number
}

export class CompareDocError extends Error {}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
const str = (v: unknown, re = ID_RE): string | null => (typeof v === 'string' && re.test(v) ? v : null)
const int = (v: unknown, lo = 0): number | null => (Number.isInteger(v) && (v as number) >= lo ? (v as number) : null)
function vec3(v: unknown): Vec3 | null {
  if (!Array.isArray(v) || v.length !== 3) return null
  const [x, y, z] = v.map(num)
  return x !== null && y !== null && z !== null ? [x, y, z] : null
}
const isMode = (v: unknown): v is SlamMode => (MODES as readonly unknown[]).includes(v)
const isSource = (v: unknown): v is CompareSource => (SOURCES as readonly unknown[]).includes(v)

/** [[x, y, z], …] → xyz 交错 float64；任一点不合规则整条丢弃 */
function polyline(v: unknown): Float64Array | null {
  if (!Array.isArray(v) || v.length < 2) return null
  const out = new Float64Array(v.length * 3)
  for (let i = 0; i < v.length; i++) {
    const p = vec3(v[i])
    if (!p) return null
    out.set(p, 3 * i)
  }
  return out
}

function parseRun(v: unknown, uavs: ReadonlySet<string>): CompareRun | null {
  if (!isObj(v)) return null
  const uav = str(v.uav)
  const rep = int(v.rep, 1)
  if (!uav || !uavs.has(uav) || !isMode(v.mode) || rep === null) return null
  const m = isObj(v.metrics) ? v.metrics : {}
  const metrics = Object.fromEntries(METRIC_KEYS.map((k) => [k, num(m[k])])) as Record<MetricKey, number | null>
  const segments: Segment[] = []
  if (Array.isArray(v.segments)) {
    for (const s of v.segments) {
      if (!isObj(s) || num(s.s_m) === null) continue
      segments.push({ sM: num(s.s_m)!, rpePct: num(s.rpe_pct), driftM: num(s.drift_m), at: vec3(s.at) })
    }
    segments.sort((a, b) => a.sM - b.sM)
  }
  const traj = isObj(v.traj) ? v.traj : {}
  return {
    uav, mode: v.mode, rep, seed: int(v.seed), ok: v.ok === true, code: int(v.code), metrics, segments,
    trajGt: polyline(traj.gt), trajEst: polyline(traj.est),
  }
}

function parseLayer(v: unknown, uavs: ReadonlySet<string>): LayerDesc | null {
  if (!isObj(v) || !isSource(v.src) || (v.tier !== 'B' && v.tier !== 'S') || !safeArtifactPath(v.path)) return null
  const sha256 = typeof v.sha256 === 'string' && /^[0-9a-f]{64}$/.test(v.sha256) ? v.sha256 : null
  if (v.kind === 'mesh') {
    const faces = int(v.faces, 1)
    if (v.src !== 'gt' || v.tier !== 'B' || faces === null) return null
    return { kind: 'mesh', src: 'gt', tier: 'B', uav: null, mode: null, rep: null, path: v.path, faces, sha256 }
  }
  if (v.kind !== 'points') return null
  const n = int(v.n, 1)
  const cubeMin = vec3(v.cube_min)
  const cubeSize = num(v.cube_size)
  const bpp = v.bpp === 12 || v.bpp === 16 ? v.bpp : null
  if (n === null || !cubeMin || cubeSize === null || !(cubeSize > 0) || bpp === null) return null
  const err = v.err === true
  if (err && bpp !== 16) return null
  const uav = v.src === 'gt' ? null : str(v.uav)
  if (v.src !== 'gt' && (!uav || !uavs.has(uav))) return null
  const mode = v.src === 'livo' ? (isMode(v.mode) ? v.mode : null) : null
  const rep = v.src === 'livo' ? int(v.rep, 1) : null
  if (v.src === 'livo' && (mode === null || rep === null)) return null
  return { kind: 'points', src: v.src, tier: v.tier, uav, mode, rep, path: v.path, n, bpp, cubeMin, cubeSize, err, sha256 }
}

function parseDist(v: unknown): DistStats | null {
  if (!isObj(v) || int(v.n) === null) return null
  return {
    n: int(v.n)!, meanM: num(v.mean_m), medianM: num(v.median_m), p90M: num(v.p90_m), p95M: num(v.p95_m), maxM: num(v.max_m),
    withinHalfDiag: num(v.within_half_diag), halfDiagM: num(v.half_diag_m),
  }
}

/** `fleet_report`（B15）的 L0 段：覆盖、两两 Jaccard 矩阵（按 `uavs` 顺序）、合并覆盖与每机到网格距离分布 */
function parseL0(v: unknown): FleetL0 | null {
  if (!isObj(v) || !Array.isArray(v.uavs)) return null
  const uavs = v.uavs.map((u) => str(u)).filter((u): u is string => u !== null)
  if (uavs.length !== v.uavs.length || !uavs.length) return null
  const cov = isObj(v.coverage) ? v.coverage : {}
  const coverage: Record<string, number> = {}
  for (const u of uavs) if (num(cov[u]) !== null) coverage[u] = num(cov[u])!
  const n = uavs.length
  const rows = Array.isArray(v.jaccard_matrix) ? v.jaccard_matrix : []
  const matrix = uavs.map((_, i) => uavs.map((__, j) => {
    const row = rows[i]
    const x = Array.isArray(row) && row.length === n ? num(row[j]) : null
    return x ?? (i === j ? 1 : Number.NaN)
  }))
  const per = isObj(v.per_uav) ? v.per_uav : {}
  const meshDistance: Record<string, DistStats> = {}
  for (const u of uavs) {
    const d = isObj(per[u]) ? parseDist(per[u].mesh_distance) : null
    if (d) meshDistance[u] = d
  }
  return {
    uavs, coverage, matrix, jaccardMax: num(v.jaccard_max), jaccardOk: typeof v.jaccard_ok === 'boolean' ? v.jaccard_ok : null,
    threshold: num(v.jaccard_threshold) ?? 0.3, merged: num(v.merged_coverage), mergedOverBest: num(v.merged_over_best), meshDistance,
  }
}

/** 校验 `metrics.json`；文档级字段不合规抛 CompareDocError，单条运行、层与抽样点不合规时丢弃并计入 `dropped` */
export function parseCompareDoc(v: unknown): CompareDoc {
  if (!isObj(v) || v.schema !== LIDAR_COMPARE_SCHEMA) throw new CompareDocError('schema')
  const runId = str(v.run_id)
  const origin = vec3(v.origin)
  if (!runId || !origin || !Array.isArray(v.uavs)) throw new CompareDocError('fields')
  const es = isObj(v.eval_sim) ? v.eval_sim : {}
  // 量化口径必须与本页反量化一致，否则着色与 eval_sim 数值不可比
  if ((es.err_cap_m !== undefined && es.err_cap_m !== ERR_CAP_M) || (es.err_step_m !== undefined && es.err_step_m !== ERR_STEP_M)) throw new CompareDocError('quantization')
  const uavs = [...new Set(v.uavs.map((u) => str(u)).filter((u): u is string => u !== null))].sort()
  if (!uavs.length) throw new CompareDocError('uavs')
  const uavSet = new Set(uavs)
  let dropped = 0
  const runs: CompareRun[] = []
  for (const r of Array.isArray(v.runs) ? v.runs : []) {
    const p = parseRun(r, uavSet)
    if (p) runs.push(p)
    else dropped++
  }
  runs.sort((a, b) => a.uav.localeCompare(b.uav) || MODES.indexOf(a.mode) - MODES.indexOf(b.mode) || a.rep - b.rep)
  const layers: LayerDesc[] = []
  for (const l of Array.isArray(v.layers) ? v.layers : []) {
    const p = parseLayer(l, uavSet)
    if (p) layers.push(p)
    else dropped++
  }
  const paths = new Set(layers.map((l) => l.path))
  const errSamples: ErrSample[] = []
  for (const s of Array.isArray(v.err_samples) ? v.err_samples : []) {
    const i = isObj(s) ? int(s.i) : null
    const d = isObj(s) ? num(s.d_m) : null
    if (isObj(s) && typeof s.layer === 'string' && paths.has(s.layer) && i !== null && d !== null && d >= 0) errSamples.push({ layer: s.layer, i, dM: d })
    else dropped++
  }
  const modes = Array.isArray(v.modes) ? MODES.filter((m) => (v.modes as unknown[]).includes(m)) : [...new Set(runs.map((r) => r.mode))]
  const crop = isObj(v.crop) && vec3(v.crop.min) && vec3(v.crop.max) ? { id: str(v.crop.id) ?? 'crop', min: vec3(v.crop.min)!, max: vec3(v.crop.max)! } : null
  const band = isObj(v.ac14) && isObj(v.ac14.realistic) && Array.isArray(v.ac14.realistic.rpe100_median_range_pct) ? v.ac14.realistic.rpe100_median_range_pct.map(num) : null
  return {
    runId, worldId: str(v.world_id), vehicle: str(v.vehicle), obs: str(v.obs), seeds: int(v.seeds, 1), repeats: int(v.repeats, 1),
    mainMode: isMode(v.main_mode) ? v.main_mode : 'livo', origin, crop, evalVersion: str(es.version), uavs,
    modes: modes.length ? modes : ['livo'], runs, l0: parseL0(v.l0), layers, errSamples,
    rpeBand: band && band.length === 2 && band[0] !== null && band[1] !== null && band[0] < band[1] ? [band[0], band[1]] : null, dropped,
  }
}

// ---------------------------------------------------------------------------------------------------- 误差量化与灰阶
/** eval_sim `mesh_error_u8` 的镜像：floor(clip(d, 0, 0.5) / 0.002 + 0.5)，float64 运算与 NumPy 逐位相同 */
export function quantizeErrU8(d: number): number {
  return Math.floor(Math.min(Math.max(d, 0), ERR_CAP_M) / ERR_STEP_M + 0.5)
}
/** u8 → 米（反量化；ERR_NONE 为 NaN） */
export function errU8ToM(u: number): number {
  return u === ERR_NONE || u > ERR_U8_MAX ? Number.NaN : u * ERR_STEP_M
}
/** 误差灰阶（§21.8.2）：0 m 为 lo（g400），0.5 m 及以上为 hi（g50），sRGB 分量线性插值；返回 0–1 的 sRGB 分量 */
export function errGray(u: number, lo: Vec3, hi: Vec3): Vec3 {
  const f = Math.min(Math.max(u, 0), ERR_U8_MAX) / ERR_U8_MAX
  return [lo[0] + (hi[0] - lo[0]) * f, lo[1] + (hi[1] - lo[1]) * f, lo[2] + (hi[2] - lo[2]) * f]
}

// ---------------------------------------------------------------------------------------------------- 点层解码与预算
export interface DecodedPoints {
  desc: PointLayerDesc
  n: number
  /** 局部 ENU（相对 origin）xyz 交错 float32 */
  pos: Float32Array
  /** sRGB u8 交错 */
  rgb: Uint8Array
  /** 误差 u8（ERR_NONE 为无值）；该层不着色时为 null */
  err: Uint8Array | null
  /** 显示时的抽稀步长（1 为全部显示；Tier S 合计预算或 Tier B 每路预算超出时 > 1） */
  stride: number
}

/** ANET_Q16 v1 单节点载荷 → 局部坐标、颜色与误差 u8；字节数与 n·bpp 不符时抛 CompareDocError */
export function decodePointLayer(buf: ArrayBuffer, desc: PointLayerDesc): DecodedPoints {
  const n = desc.n
  if (buf.byteLength !== n * desc.bpp) throw new CompareDocError(`size ${desc.path}`)
  const q = new Uint16Array(buf, 0, 4 * n)
  const col = new Uint8Array(buf, 8 * n, 4 * n)
  const ext = desc.bpp === 16 ? new Uint8Array(buf, 12 * n, 4 * n) : null
  const pos = new Float32Array(3 * n)
  const rgb = new Uint8Array(3 * n)
  const err = desc.err && ext ? new Uint8Array(n) : null
  const [mx, my, mz] = desc.cubeMin
  const s = desc.cubeSize / 65535
  for (let k = 0; k < n; k++) {
    pos[3 * k] = mx + q[4 * k]! * s
    pos[3 * k + 1] = my + q[4 * k + 1]! * s
    pos[3 * k + 2] = mz + q[4 * k + 2]! * s
    rgb[3 * k] = col[4 * k]!
    rgb[3 * k + 1] = col[4 * k + 1]!
    rgb[3 * k + 2] = col[4 * k + 2]!
    if (err) {
      const u = ext![4 * k]!
      err[k] = u > ERR_U8_MAX ? ERR_NONE : u
    }
  }
  return { desc, n, pos, rgb, err, stride: 1 }
}

/** 抽稀步长：每 stride 个点取一个（下标 0, stride, 2·stride…），显示点数 ceil(n / stride) ≤ target */
export function strideFor(n: number, target: number): number {
  return n <= target ? 1 : Math.ceil(n / Math.max(1, Math.floor(target)))
}
export const shownCount = (n: number, stride: number): number => Math.ceil(n / stride)

/**
 * 各显示层的抽稀步长：Tier B 每路 ≤ 2,000,000 点；Tier S 合计 ≤ TIER_S_POINTS_TOTAL，按点数比例分配（每层 target = floor(n·B/Σn)，
 * ceil(n / ceil(n / target)) ≤ target，合计不超过预算）。制品已按阶梯预抽稀时步长全为 1。
 */
export function budgetStrides(ns: readonly number[], tier: CompareTier): number[] {
  if (tier === 'B') return ns.map((n) => strideFor(n, TIER_B_POINTS_PER_MAP))
  const sum = ns.reduce((a, b) => a + b, 0)
  if (sum <= TIER_S_POINTS_TOTAL) return ns.map(() => 1)
  return ns.map((n) => strideFor(n, Math.max(1, Math.floor((n * TIER_S_POINTS_TOTAL) / sum))))
}

/** 网格面数预算（Tier B ≤ 300,000 面）；超出的网格不显示 */
export const meshWithinBudget = (faces: number): boolean => faces <= TIER_B_MESH_FACES

// ---------------------------------------------------------------------------------------------------- 统计
/** NumPy `percentile` 的缺省（linear）插值；`sorted` 升序 */
export function quantileSorted(sorted: ArrayLike<number>, p: number): number {
  const n = sorted.length
  if (!n) return Number.NaN
  const h = (n - 1) * p
  const lo = Math.floor(h)
  const hi = Math.min(lo + 1, n - 1)
  return sorted[lo]! + (h - lo) * (sorted[hi]! - sorted[lo]!)
}

export interface BoxStats { n: number; min: number; p10: number; q1: number; median: number; q3: number; p90: number; max: number; values: number[] }
/** 箱线图（§21.8.3）：箱为四分位，须为 p10–p90，点为单次；分位数口径同 slam_eval `distribution`（NumPy linear） */
export function boxStats(values: readonly (number | null)[]): BoxStats | null {
  const v = values.filter((x): x is number => x !== null && Number.isFinite(x)).sort((a, b) => a - b)
  if (!v.length) return null
  return {
    n: v.length, min: v[0]!, p10: quantileSorted(v, 0.1), q1: quantileSorted(v, 0.25), median: quantileSorted(v, 0.5), q3: quantileSorted(v, 0.75),
    p90: quantileSorted(v, 0.9), max: v[v.length - 1]!, values: v,
  }
}

/** 某机某模式全部成功运行（种子 × 重复）的一项指标 */
export function metricValues(doc: CompareDoc, uav: string, mode: SlamMode, key: MetricKey): (number | null)[] {
  return doc.runs.filter((r) => r.uav === uav && r.mode === mode && r.ok).map((r) => r.metrics[key])
}
/** 主指标：跨"种子 × 重复"的中位数（单位与条件写在副文） */
export function medianOf(doc: CompareDoc, uav: string, mode: SlamMode, key: MetricKey): number | null {
  return boxStats(metricValues(doc, uav, mode, key))?.median ?? null
}

export interface ErrStats { n: number; none: number; meanM: number; medianM: number; p90M: number; maxM: number }
/** 误差 u8 序列的统计（米，反量化后）；无值点只计入 `none` */
export function errStats(values: readonly number[]): ErrStats {
  const v: number[] = []
  let none = 0
  for (const u of values) {
    if (u === ERR_NONE || u > ERR_U8_MAX) none++
    else v.push(u * ERR_STEP_M)
  }
  v.sort((a, b) => a - b)
  const mean = v.length ? v.reduce((a, b) => a + b, 0) / v.length : Number.NaN
  return { n: v.length, none, meanM: mean, medianM: quantileSorted(v, 0.5), p90M: quantileSorted(v, 0.9), maxM: v.length ? v[v.length - 1]! : Number.NaN }
}

export interface ErrCheck { n: number; missing: number; maxAbsDiffM: number | null; ok: boolean | null }
/**
 * 文档自带的抽样点核对（UX-AC-095"误差着色与 metrics.json 一致，抽样 1,000 点，量化误差 ≤ 2 mm"）：随点下发的 u8 反量化值与 eval_sim
 * 的浮点距离之差；超过 0.5 m 的样本按截断值比较。层未加载或下标越界计入 `missing`。
 */
export function checkErrSamples(samples: readonly ErrSample[], layers: ReadonlyMap<string, DecodedPoints>): ErrCheck {
  let n = 0
  let missing = 0
  let worst = 0
  for (const s of samples) {
    const L = layers.get(s.layer)
    const u = L?.err && s.i < L.n ? L.err[s.i]! : null
    if (u === null || u === ERR_NONE) {
      missing++
      continue
    }
    n++
    worst = Math.max(worst, Math.abs(u * ERR_STEP_M - Math.min(s.dM, ERR_CAP_M)))
  }
  return { n, missing, maxAbsDiffM: n ? worst : null, ok: n ? worst <= ERR_CHECK_TOL_M : null }
}

// ---------------------------------------------------------------------------------------------------- 投影、点选与框选
/**
 * 局部坐标 → 画布像素（左上为原点）。`m` 为列主序 4×4 视图投影矩阵（three.js Matrix4.elements）；位于相机后方或裁剪空间外返回 null。
 */
export function projectPx(m: ArrayLike<number>, x: number, y: number, z: number, w: number, h: number, out: Float64Array): boolean {
  const cw = m[3]! * x + m[7]! * y + m[11]! * z + m[15]!
  if (!(cw > 0)) return false
  const nx = (m[0]! * x + m[4]! * y + m[8]! * z + m[12]!) / cw
  const ny = (m[1]! * x + m[5]! * y + m[9]! * z + m[13]!) / cw
  const nz = (m[2]! * x + m[6]! * y + m[10]! * z + m[14]!) / cw
  if (nz < -1 || nz > 1) return false
  out[0] = ((nx + 1) / 2) * w
  out[1] = ((1 - ny) / 2) * h
  out[2] = nz
  return true
}

export interface PickHit { layer: number; i: number; distPx: number; depth: number }
/** 点选：各层显示点中离 (px, py) 最近且 ≤ radiusPx 的点；屏幕距离相同时取更近的深度 */
export function pickNearest(layers: readonly DecodedPoints[], m: ArrayLike<number>, w: number, h: number, px: number, py: number, radiusPx: number): PickHit | null {
  const o = new Float64Array(3)
  let best: PickHit | null = null
  layers.forEach((L, li) => {
    for (let i = 0; i < L.n; i += L.stride) {
      if (!projectPx(m, L.pos[3 * i]!, L.pos[3 * i + 1]!, L.pos[3 * i + 2]!, w, h, o)) continue
      const d = Math.hypot(o[0]! - px, o[1]! - py)
      if (d > radiusPx) continue
      if (!best || d < best.distPx - 1e-9 || (Math.abs(d - best.distPx) <= 1e-9 && o[2]! < best.depth)) best = { layer: li, i, distPx: d, depth: o[2]! }
    }
  })
  return best
}

export interface Rect { x0: number; y0: number; x1: number; y1: number }
/** 框选：投影落在矩形内（含边界）的显示点的误差 u8，各层合并统计 */
export function rectErrStats(layers: readonly DecodedPoints[], m: ArrayLike<number>, w: number, h: number, r: Rect): ErrStats & { points: number } {
  const o = new Float64Array(3)
  const x0 = Math.min(r.x0, r.x1)
  const x1 = Math.max(r.x0, r.x1)
  const y0 = Math.min(r.y0, r.y1)
  const y1 = Math.max(r.y0, r.y1)
  const us: number[] = []
  let points = 0
  for (const L of layers) {
    for (let i = 0; i < L.n; i += L.stride) {
      if (!projectPx(m, L.pos[3 * i]!, L.pos[3 * i + 1]!, L.pos[3 * i + 2]!, w, h, o)) continue
      if (o[0]! < x0 || o[0]! > x1 || o[1]! < y0 || o[1]! > y1) continue
      points++
      if (L.err) us.push(L.err[i]!)
    }
  }
  return { ...errStats(us), points }
}

// ---------------------------------------------------------------------------------------------------- URL 与选择
export interface CompareSearch {
  uav: string | null
  src: CompareSource[]
  rep: number | null
  mode: SlamMode | null
  /** 显示档覆盖（S、B；缺省按设备类别：软件光栅为 S，其余为 B） */
  tier: CompareTier | null
  color: ColorBy
  size: number
  /** 是否显示裁剪框（`crop=0` 隐藏） */
  crop: boolean
}
export const POINT_SIZES = [1, 2, 3, 4, 6] as const
/** `uav`、`src`（gt,l0,livo 的子集，`none` 为全部关闭）、`rep`、`mode`（AWR-14 §21.2.3 路由表）与 `tier`、`color`、`size`、`crop` */
export function parseCompareSearch(s: URLSearchParams): CompareSearch {
  const srcRaw = s.get('src')
  const src = srcRaw === null ? [...SOURCES] : SOURCES.filter((x) => srcRaw.split(',').includes(x))
  const rep = Number(s.get('rep'))
  const tier = s.get('tier')
  const size = Number(s.get('size'))
  return {
    uav: str(s.get('uav')), src, rep: Number.isInteger(rep) && rep >= 1 ? rep : null, mode: isMode(s.get('mode')) ? (s.get('mode') as SlamMode) : null,
    tier: tier === 'S' || tier === 'B' ? tier : null, color: s.get('color') === 'rgb' ? 'rgb' : 'err',
    size: (POINT_SIZES as readonly number[]).includes(size) ? size : 2, crop: s.get('crop') !== '0',
  }
}

/** 设备档到对比视图显示档：Tier A 与 B 同一套（裁剪网格），Tier S 点云降级 */
export const compareTierOf = (t: Tier): CompareTier => (t === 'S' ? 'S' : 'B')

export interface Selection { uav: string; mode: SlamMode; rep: number | null; run: CompareRun | null; reps: number[] }
/** 由 URL 与文档确定所选机、模式与重复（不合规值回落到第一架机、主模式、该机该模式的第一个重复） */
export function resolveSelection(doc: CompareDoc, q: CompareSearch): Selection {
  const uav = q.uav && doc.uavs.includes(q.uav) ? q.uav : doc.uavs[0]!
  const mode = q.mode && doc.modes.includes(q.mode) ? q.mode : doc.modes.includes(doc.mainMode) ? doc.mainMode : doc.modes[0]!
  const reps = [...new Set(doc.runs.filter((r) => r.uav === uav && r.mode === mode).map((r) => r.rep))].sort((a, b) => a - b)
  const rep = q.rep !== null && reps.includes(q.rep) ? q.rep : (reps[0] ?? null)
  const run = doc.runs.find((r) => r.uav === uav && r.mode === mode && r.rep === rep) ?? null
  return { uav, mode, rep, run, reps }
}

/** 所选机在某显示档下要加载的层：真值（Tier B 网格，Tier S 采样点云）、该机 L0 地图、该机该模式该重复的 FAST-LIVO2 地图 */
export function layersFor(doc: CompareDoc, sel: Selection, tier: CompareTier, src: readonly CompareSource[]): LayerDesc[] {
  const out: LayerDesc[] = []
  const want = new Set(src)
  const of = (l: LayerDesc) => l.tier === tier
  if (want.has('gt')) {
    const gt = doc.layers.find((l) => of(l) && l.src === 'gt' && l.kind === (tier === 'B' ? 'mesh' : 'points'))
    if (gt) out.push(gt)
  }
  if (want.has('l0')) {
    const l0 = doc.layers.find((l) => of(l) && l.src === 'l0' && l.uav === sel.uav)
    if (l0) out.push(l0)
  }
  if (want.has('livo') && sel.rep !== null) {
    const lv = doc.layers.find((l) => of(l) && l.src === 'livo' && l.uav === sel.uav && l.mode === sel.mode && l.rep === sel.rep)
    if (lv) out.push(lv)
  }
  return out
}

/** 制品下载地址（BN-54）：`/runs/{rid}/artifacts/compare/{path}`；路径已在解析时校验 */
export const compareArtifactPath = (rid: string, path: string): string => `/runs/${encodeURIComponent(rid)}/artifacts/compare/${path}`
export const COMPARE_DOC = 'metrics.json'
