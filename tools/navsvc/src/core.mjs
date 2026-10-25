// NavMesh 查询核心（ADR-196 决策 (3)、(8)；M22 §6.8、§7.3.3）：Node 端 navsvc 与浏览器端编辑器（D3-AC-26）共用的同构模块。
//
// 本模块不引用 node:* 内置模块：调用方以 initRecastFromBinary(wasmBytes) 装载同一份 recast-navigation 0.43.1 WASM
// （Node 由 recast.mjs 读文件并核对 sha256，浏览器以 fetch 取同一文件），随后以 NavMesh 二进制（exportNavMesh 字节）与
// 图层清单（`<profile>.json`）构造 NavQuery。
//
// 坐标约定：对外一律为世界帧 (E, N, U) 整数毫米；Recast 轴向 (x, y, z) = (E, U, −N)（米）的换算只在本模块内进行，
// 输出在边界上以 Math.round 量化为整数毫米，故同一请求序列的应答逐字节相同。

import {
  Detour, NavMeshQuery, QueryFilter, importNavMesh, init, setRandomSeed, statusFailed,
} from '@recast-navigation/core'
import wasmFactory from '@recast-navigation/wasm/wasm'

// Detour 区域与代价（M22 §6.8；与 python/awr/world/glb/derive/navmesh.py AREAS 一致）
export const AREAS = [
  { id: 1, name: 'ROAD_CORRIDOR', cost: 1.0, walkable: true },
  { id: 2, name: 'PAVED', cost: 1.5, walkable: true },
  { id: 3, name: 'GROUND', cost: 3.0, walkable: true },
  { id: 4, name: 'VEGETATION', cost: 8.0, walkable: true },
  { id: 5, name: 'STEEP', cost: 6.0, walkable: true },
  { id: 6, name: 'OFFMESH', cost: 1.0, walkable: true },
]
// Detour 标志位（M22 §6.8）；裁剪掉的多边形 flags = 0，任何过滤器都不再包含
export const FLAGS = { walk: 1, drive: 2, door: 4, disabled: 8 }
export const PROFILE_FLAG = { person: FLAGS.walk, vehicle: FLAGS.drive }
export const SNAP_EXT_MM = [2000, 4000, 2000]          // 初次吸附搜索半宽（Recast x、y（竖直）、z），M22 §6.8
export const MAX_NODES = 65535                         // 查询 maxNodes（M22 §6.8）
export const MAX_PATH_POLYS = 8192
export const MAX_STRAIGHT_POINTS = 8192
export const MAX_PATH_ROUNDS = 16                   // A* 节点池耗尽后的续搜段数上限

let ready = null

/** 以 WASM 字节装载 recast-navigation（单线程、不含 SIMD 的 MVP 二进制；同一进程只装载一次）。 */
export function initRecastFromBinary(wasmBinary) {
  if (!ready) ready = init(() => wasmFactory({ wasmBinary }))
  return ready
}

/** 世界帧整数毫米 [E, N, U] → Recast 米 {x, y, z}。 */
export function toRecast(p) {
  return { x: p[0] / 1000, y: p[2] / 1000, z: -p[1] / 1000 }
}

/** Recast 米 {x, y, z} → 世界帧整数毫米 [E, N, U]（Math.round；−0 由 JSON 序列化为 0）。 */
export function fromRecast(v) {
  return [Math.round(v.x * 1000) + 0, Math.round(-v.z * 1000) + 0, Math.round(v.y * 1000) + 0]
}

/** mulberry32：32 位整数状态的确定性伪随机数，返回 [0, 1)。 */
export function mulberry32(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function hypot2(a, b) {
  return Math.round(Math.hypot(a[0] - b[0], a[1] - b[1]))
}

function len3(points) {
  let s = 0
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1]; const b = points[i]
    s += Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2])
  }
  return Math.round(s)
}

function isMm(p) {
  return Array.isArray(p) && p.length === 3 && p.every((v) => Number.isSafeInteger(v))
}

export class NavError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code
  }
}

function needMm(name, p) {
  if (!isMm(p)) throw new NavError('BAD_REQUEST', `${name} must be [E, N, U] integer millimetres`)
}

/** 构造查询过滤器：includeFlags 取 profile 标志位，excludeFlags 取 disabled；区域代价取图层清单 `areas[]`（缺省 §6.8）。 */
export function makeFilter(profile, areas = AREAS) {
  const f = new QueryFilter()
  f.includeFlags = PROFILE_FLAG[profile] ?? 0xffff
  f.excludeFlags = FLAGS.disabled
  for (const a of areas) if (a.walkable && a.id > 0 && Number.isFinite(a.cost)) f.setAreaCost(a.id, a.cost)
  return f
}

/**
 * 单个 profile 的 Detour 查询对象（snap、path、los、random_point_around、height）。
 * `doc` 为图层清单（`<profile>.json`），只读取 `profile` 与 `areas`。
 */
export class NavQuery {
  constructor(bytes, doc) {
    const { navMesh } = importNavMesh(bytes)
    this.navMesh = navMesh
    this.profile = doc.profile
    this.filter = makeFilter(doc.profile, doc.areas || AREAS)
    this.query = new NavMeshQuery(navMesh, { maxNodes: MAX_NODES, defaultQueryFilter: this.filter })
  }

  destroy() {
    this.query.destroy()
    this.navMesh.destroy()
  }

  _nearest(p, ext) {
    const e = ext ?? SNAP_EXT_MM
    const r = this.query.findNearestPoly(toRecast(p), {
      filter: this.filter, halfExtents: { x: e[0] / 1000, y: e[1] / 1000, z: e[2] / 1000 },
    })
    if (!r.success || !r.nearestRef) return null
    return { ref: r.nearestRef, pt: r.nearestPoint, over: r.isOverPoly }
  }

  /** findNearestPoly：`{found, ref, p, dist_mm（水平）, over}`。 */
  snap(p, ext) {
    needMm('p', p)
    const n = this._nearest(p, ext)
    if (!n) return { found: false }
    const q = fromRecast(n.pt)
    return { found: true, ref: n.ref, p: q, dist_mm: hypot2(p, q), over: n.over }
  }

  /**
   * findPath + findStraightPath：`complete` 为真表示走廊抵达终点多边形（无 DT_PARTIAL_RESULT）；否则 `why` 为 `island`
   * （终点不连通）或 `nodes`（续搜段数用尽）。`points` 为拉直路径点（世界帧毫米，含起止吸附点），`length_mm` 为折线三维长度，
   * `rounds` 为 A* 搜索段数（1 为单次搜索）。
   */
  path(a, b, ext) {
    needMm('a', a)
    needMm('b', b)
    const s = this._nearest(a, ext)
    if (!s) return { found: false, why: 'start' }
    const e = this._nearest(b, ext)
    if (!e) return { found: false, why: 'end' }
    // 走廊：A* 节点池（dtNodePool 以 16 位下标，maxNodes ≤ 65535）耗尽时返回 DT_OUT_OF_NODES | DT_PARTIAL_RESULT，
    // 此时从部分走廊的末多边形续搜（至多 MAX_PATH_ROUNDS 段），拼接后的走廊相邻多边形首尾相接，仍可整体拉直。
    const corridor = []
    let from = s
    let rounds = 0
    let complete = false
    let outOfNodes = false
    while (rounds < MAX_PATH_ROUNDS) {
      rounds++
      const fp = this.query.findPath(from.ref, e.ref, from.pt, e.pt, { filter: this.filter, maxPathPolys: MAX_PATH_POLYS })
      const n = fp.polys.size
      if (statusFailed(fp.status) || n === 0) {
        fp.polys.destroy()
        break
      }
      for (let i = corridor.length ? 1 : 0; i < n; i++) corridor.push(fp.polys.get(i))
      const last = fp.polys.get(n - 1)
      const st = fp.status
      fp.polys.destroy()
      if (last === e.ref && (st & Detour.DT_PARTIAL_RESULT) === 0) {
        complete = true
        break
      }
      outOfNodes = (st & Detour.DT_OUT_OF_NODES) !== 0
      if (!outOfNodes || corridor.length >= MAX_PATH_POLYS || last === from.ref) break
      const c = this.query.closestPointOnPoly(last, e.pt)
      if (!c.success) break
      from = { ref: last, pt: c.closestPoint }
    }
    if (corridor.length === 0) return { found: false, why: 'path' }
    const lastRef = corridor[corridor.length - 1]
    let endPt = e.pt
    if (!complete) {
      const c = this.query.closestPointOnPoly(lastRef, e.pt)
      endPt = c.success ? c.closestPoint : s.pt
    }
    const sp = this.query.findStraightPath(s.pt, endPt, corridor.slice(0, MAX_PATH_POLYS), { maxStraightPathPoints: MAX_STRAIGHT_POINTS })
    const pts = []
    for (let i = 0; i < sp.straightPathCount; i++) {
      pts.push(fromRecast({ x: sp.straightPath.get(3 * i), y: sp.straightPath.get(3 * i + 1), z: sp.straightPath.get(3 * i + 2) }))
    }
    sp.straightPath.destroy()
    sp.straightPathFlags.destroy()
    sp.straightPathRefs.destroy()
    const out = { found: true, complete, n_polys: corridor.length, rounds, points: pts, length_mm: len3(pts) }
    if (!complete) out.why = outOfNodes ? 'nodes' : 'island'
    return out
  }

  /** raycast（沿可走面的二维视线）：`clear` 为真表示无边界阻挡到达终点；否则给出命中参数 t（百万分之一）与命中点。 */
  los(a, b, ext) {
    needMm('a', a)
    needMm('b', b)
    const s = this._nearest(a, ext)
    if (!s) return { found: false, why: 'start' }
    const end = toRecast(b)
    const r = this.query.raycast(s.ref, s.pt, end, { filter: this.filter })
    if (statusFailed(r.status)) return { found: false, why: 'raycast' }
    if (!(r.t < 1)) return { found: true, clear: true, t_ppm: 1000000, hit: null }
    const hit = { x: s.pt.x + (end.x - s.pt.x) * r.t, y: s.pt.y + (end.y - s.pt.y) * r.t, z: s.pt.z + (end.z - s.pt.z) * r.t }
    return { found: true, clear: false, t_ppm: Math.round(r.t * 1e6), hit: fromRecast(hit) }
  }

  /** findRandomPointAroundCircle：每次调用前以调用方给出的种子重置 Detour FastRand（结果只取决于请求本身）。 */
  randomPointAround(p, rMm, seed, ext) {
    needMm('p', p)
    if (!Number.isSafeInteger(rMm) || rMm <= 0) throw new NavError('BAD_REQUEST', 'r_mm must be a positive integer')
    if (!Number.isSafeInteger(seed) || seed < 0 || seed > 0xffffffff) throw new NavError('BAD_REQUEST', 'seed must be a uint32')
    const s = this._nearest(p, ext)
    if (!s) return { found: false, why: 'start' }
    setRandomSeed(seed)
    const r = this.query.findRandomPointAroundCircle(s.pt, rMm / 1000, { startRef: s.ref, filter: this.filter })
    if (!r.success || !r.randomPolyRef) return { found: false, why: 'random' }
    return { found: true, ref: r.randomPolyRef, p: fromRecast(r.randomPoint) }
  }

  /**
   * 可走面上按面积均匀取点（D3-AC-03 可达性统计抽样）：多边形按面积累积分布抽取，多边形内按三角扇面积抽三角形、以平方根法
   * 取重心坐标，高度取 getPolyHeight。随机数为以请求种子初始化的 mulberry32，与 Detour FastRand 无关（findRandomPoint 先等概率
   * 选瓦片，不按面积均匀，且选中全部被裁剪的瓦片时失败）。
   */
  randomPoint(seed) {
    if (!Number.isSafeInteger(seed) || seed < 0 || seed > 0xffffffff) throw new NavError('BAD_REQUEST', 'seed must be a uint32')
    const t = this._areaTable()
    if (!t.total) return { found: false }
    const rnd = mulberry32(seed)
    const u = rnd() * t.total
    let lo = 0; let hi = t.cum.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (t.cum[mid] > u) hi = mid
      else lo = mid + 1
    }
    const { tile, poly } = this.navMesh.getTileAndPolyByRefUnsafe(t.refs[lo])
    const n = poly.vertCount()
    const P = []
    for (let j = 0; j < n; j++) {
      const v = poly.verts(j) * 3
      P.push([tile.verts(v), tile.verts(v + 1), tile.verts(v + 2)])
    }
    const w = []
    let sw = 0
    for (let j = 1; j < n - 1; j++) {
      const a = Math.abs((P[j][0] - P[0][0]) * (P[j + 1][2] - P[0][2]) - (P[j + 1][0] - P[0][0]) * (P[j][2] - P[0][2])) / 2
      sw += a
      w.push(sw)
    }
    const ut = rnd() * sw
    let k = 0
    while (k < w.length - 1 && w[k] <= ut) k++
    const r1 = Math.sqrt(rnd()); const r2 = rnd()
    const A = P[0]; const B = P[k + 1]; const C = P[k + 2]
    const x = (1 - r1) * A[0] + r1 * (1 - r2) * B[0] + r1 * r2 * C[0]
    const z = (1 - r1) * A[2] + r1 * (1 - r2) * B[2] + r1 * r2 * C[2]
    const y = (1 - r1) * A[1] + r1 * (1 - r2) * B[1] + r1 * r2 * C[1]
    const h = this.query.getPolyHeight(t.refs[lo], { x, y, z })
    return { found: true, ref: t.refs[lo], p: fromRecast({ x, y: h.success ? h.height : y, z }) }
  }

  _areaTable() {
    if (this._areas) return this._areas
    const refs = []; const cum = []
    let total = 0
    const inc = this.filter.includeFlags; const exc = this.filter.excludeFlags
    for (let ti = 0; ti < this.navMesh.getMaxTiles(); ti++) {
      const tile = this.navMesh.getTile(ti)
      const h = tile.header()
      if (!h) continue
      const base = this.navMesh.getPolyRefBase(tile)
      for (let i = 0; i < h.polyCount(); i++) {
        const poly = tile.polys(i)
        const f = poly.flags()
        if (poly.getType() !== 0 || (f & inc) === 0 || (f & exc) !== 0) continue
        const a = polyArea(tile, poly)
        if (!(a > 0)) continue
        total += a
        refs.push(base + i)
        cum.push(total)
      }
    }
    this._areas = { refs, cum, total }
    return this._areas
  }

  /** 导航面高度（getPolyHeight，Detour 细节网格）：作为 `walkable_height` 的 `nav_z` 输入（M22 §6.6.14）。 */
  height(p, ext) {
    needMm('p', p)
    const s = this._nearest(p, ext)
    if (!s) return { found: false }
    const pos = s.over ? { x: p[0] / 1000, y: s.pt.y, z: -p[1] / 1000 } : s.pt
    const h = this.query.getPolyHeight(s.ref, pos)
    if (!h.success) return { found: false }
    return { found: true, ref: s.ref, z_mm: Math.round(h.height * 1000) + 0, over: s.over }
  }
}

/** 按图层清单 `islands` 的口径统计：多边形数、标志位非 0 的多边形数与面积（Recast xz 平面多边形面积，m²）。 */
export function meshSummary(navMesh) {
  let tiles = 0; let polys = 0; let kept = 0; let area = 0
  for (let ti = 0; ti < navMesh.getMaxTiles(); ti++) {
    const tile = navMesh.getTile(ti)
    const h = tile.header()
    if (!h) continue
    tiles++
    for (let i = 0; i < h.polyCount(); i++) {
      const poly = tile.polys(i)
      polys++
      if (poly.flags() === 0 || poly.getType() !== 0) continue
      kept++
      area += polyArea(tile, poly)
    }
  }
  return { tiles, polys, polys_kept: kept, area_kept_m2: area }
}

/** 多边形在 Recast xz 平面上的面积（顶点按 Detour 顺序，鞋带公式，m²）。 */
export function polyArea(tile, poly) {
  const n = poly.vertCount()
  let s = 0
  for (let j = 0; j < n; j++) {
    const a = poly.verts(j) * 3; const b = poly.verts((j + 1) % n) * 3
    s += tile.verts(a) * tile.verts(b + 2) - tile.verts(b) * tile.verts(a + 2)
  }
  return Math.abs(s) / 2
}
