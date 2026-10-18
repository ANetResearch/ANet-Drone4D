// Walkable surface height (M22-FR-046, M22-AC-015; D3-AC-07 "walkable_height Python and JS implementations agree with the
// golden bit for bit"). Mirror of awr.world.mesh.query.WalkIndex / MeshWorldQuery.walkable_height. Owner: M06.
// The input is the awr-trimesh@1 raycast layer: xyz.f32 (tile-local float32 vertices), tri.u32 (tile-local vertex indices)
// and the index.json tile table. From the navigation-surface height + 2 m a vertical ray is cast downwards and the first
// mesh hit is returned. The hit test is defined with explicit float64 arithmetic (no Embree), so every result matches
// Python bit for bit (golden packages/contracts/golden/event/walkable-small.*):
//   vertex = float64(xyz[v0 + i]) + origin (per component);
//   triangle g hits (x, y) iff (x, y) lies in the closed plan bounding box of its vertices, the plan determinant
//   |d| >= 1e-12 and all three barycentric weights are >= -1e-9; z = l1·z1 + l2·z2 + l3·z3 (summed left to right);
//   among hits with z <= zTop the largest z wins, ties go to the smallest global triangle number.
// The 2 m cell index only prunes candidates and never changes a result.

export const WALK_ABOVE_M = 2.0
export const WALK_CELL_M = 2.0
const BARY_EPS = 1e-9
const DET_MIN = 1e-12

/** One entry of the awr-trimesh@1 index.json `tiles` table. */
export interface WalkTile {
  origin_m: readonly [number, number, number] | readonly number[]
  v0: number
  nv: number
  t0: number
  nt: number
}

/** Downward hit: z in m (NaN when nothing is hit) and the global triangle number (-1 when nothing is hit). */
export interface WalkHit {
  z: number
  prim: number
}

interface TileGrid {
  ox: number
  oy: number
  oz: number
  v0: number
  lox: number
  loy: number
  hix: number
  hiy: number
  gx0: number
  gy0: number
  gnx: number
  start: Int32Array // gnx·gny + 1
  items: Int32Array // global triangle numbers, ascending within a cell
}

export class WalkMesh {
  private readonly grids: TileGrid[]

  constructor(
    readonly xyz: Float32Array,
    readonly tri: Uint32Array,
    tiles: readonly WalkTile[],
  ) {
    this.grids = tiles.map((t) => this.buildTile(t))
  }

  private vx(v0: number, i: number, c: number, o: number): number {
    return this.xyz[3 * (v0 + i) + c] + o
  }

  private buildTile(t: WalkTile): TileGrid {
    const [ox, oy, oz] = [t.origin_m[0], t.origin_m[1], t.origin_m[2]]
    const n = t.nt
    const span = new Int32Array(4 * n)
    const mn = new Float64Array(2 * n)
    const mx = new Float64Array(2 * n)
    let lox = Infinity
    let loy = Infinity
    let hix = -Infinity
    let hiy = -Infinity
    for (let j = 0; j < n; j++) {
      const g = t.t0 + j
      let ax = Infinity
      let ay = Infinity
      let bx = -Infinity
      let by = -Infinity
      for (let c = 0; c < 3; c++) {
        const i = this.tri[3 * g + c]
        const x = this.vx(t.v0, i, 0, ox)
        const y = this.vx(t.v0, i, 1, oy)
        ax = Math.min(ax, x)
        ay = Math.min(ay, y)
        bx = Math.max(bx, x)
        by = Math.max(by, y)
      }
      mn[2 * j] = ax
      mn[2 * j + 1] = ay
      mx[2 * j] = bx
      mx[2 * j + 1] = by
      lox = Math.min(lox, ax)
      loy = Math.min(loy, ay)
      hix = Math.max(hix, bx)
      hiy = Math.max(hiy, by)
    }
    const empty = n === 0
    const gx0 = empty ? 0 : Math.floor(lox / WALK_CELL_M)
    const gy0 = empty ? 0 : Math.floor(loy / WALK_CELL_M)
    const gnx = empty ? 0 : Math.floor(hix / WALK_CELL_M) - gx0 + 1
    const gny = empty ? 0 : Math.floor(hiy / WALK_CELL_M) - gy0 + 1
    const start = new Int32Array(gnx * gny + 1)
    for (let j = 0; j < n; j++) {
      span[4 * j] = Math.floor(mn[2 * j] / WALK_CELL_M) - gx0
      span[4 * j + 1] = Math.floor(mx[2 * j] / WALK_CELL_M) - gx0
      span[4 * j + 2] = Math.floor(mn[2 * j + 1] / WALK_CELL_M) - gy0
      span[4 * j + 3] = Math.floor(mx[2 * j + 1] / WALK_CELL_M) - gy0
      for (let cy = span[4 * j + 2]; cy <= span[4 * j + 3]; cy++) {
        for (let cx = span[4 * j]; cx <= span[4 * j + 1]; cx++) start[cy * gnx + cx + 1]++
      }
    }
    for (let c = 0; c < gnx * gny; c++) start[c + 1] += start[c]
    const items = new Int32Array(start[gnx * gny])
    const fill = start.slice(0, gnx * gny)
    for (let j = 0; j < n; j++) {
      for (let cy = span[4 * j + 2]; cy <= span[4 * j + 3]; cy++) {
        for (let cx = span[4 * j]; cx <= span[4 * j + 1]; cx++) items[fill[cy * gnx + cx]++] = t.t0 + j
      }
    }
    return { ox, oy, oz, v0: t.v0, lox, loy, hix, hiy, gx0, gy0, gnx, start, items }
  }

  /** First mesh hit of the vertical ray from (x, y, zTop) downwards; zTop NaN means no navigation surface. */
  surface(x: number, y: number, zTop: number): WalkHit {
    let bz = 0
    let bid = -1
    if (Number.isNaN(zTop)) return { z: NaN, prim: -1 }
    const { xyz, tri } = this
    for (const t of this.grids) {
      if (x < t.lox || x > t.hix || y < t.loy || y > t.hiy) continue
      const c = (Math.floor(y / 2.0) - t.gy0) * t.gnx + (Math.floor(x / 2.0) - t.gx0)
      for (let s = t.start[c]; s < t.start[c + 1]; s++) {
        const g = t.items[s]
        const i1 = 3 * (t.v0 + tri[3 * g])
        const i2 = 3 * (t.v0 + tri[3 * g + 1])
        const i3 = 3 * (t.v0 + tri[3 * g + 2])
        const x1 = xyz[i1] + t.ox
        const y1 = xyz[i1 + 1] + t.oy
        const z1 = xyz[i1 + 2] + t.oz
        const x2 = xyz[i2] + t.ox
        const y2 = xyz[i2 + 1] + t.oy
        const z2 = xyz[i2 + 2] + t.oz
        const x3 = xyz[i3] + t.ox
        const y3 = xyz[i3 + 1] + t.oy
        const z3 = xyz[i3 + 2] + t.oz
        if (x < Math.min(x1, x2, x3) || x > Math.max(x1, x2, x3) || y < Math.min(y1, y2, y3) || y > Math.max(y1, y2, y3)) continue
        const d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if (Math.abs(d) < DET_MIN) continue
        const l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
        const l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
        const l3 = 1 - l1 - l2
        if (l1 < -BARY_EPS || l2 < -BARY_EPS || l3 < -BARY_EPS) continue
        const z = l1 * z1 + l2 * z2 + l3 * z3
        if (!(z <= zTop)) continue
        if (bid < 0 || z > bz || (z === bz && g < bid)) {
          bz = z
          bid = g
        }
      }
    }
    return bid < 0 ? { z: NaN, prim: -1 } : { z: bz, prim: bid }
  }

  /** float64 heights and triangle numbers for interleaved points xy = [x0, y0, x1, y1, …] and per-point ray starts. */
  surfaceBatch(xy: ArrayLike<number>, zTop: ArrayLike<number>): { z: Float64Array; prim: Int32Array } {
    const n = xy.length >> 1
    const z = new Float64Array(n)
    const prim = new Int32Array(n)
    for (let k = 0; k < n; k++) {
      const h = this.surface(xy[2 * k], xy[2 * k + 1], zTop[k])
      z[k] = h.z
      prim[k] = h.prim
    }
    return { z, prim }
  }

  /**
   * Walkable height (float32, NaN when there is no navigation surface or no hit) at interleaved points xy: the first mesh
   * hit below navZ + 2 m. Without navZ the ray starts at +∞ (the highest surface), the transitional reading used until the
   * NavMesh layer ships, as in Python.
   */
  walkableHeight(xy: ArrayLike<number>, navZ?: ArrayLike<number>): Float32Array {
    const n = xy.length >> 1
    const out = new Float32Array(n)
    for (let k = 0; k < n; k++) {
      const top = navZ === undefined ? Infinity : navZ[k] + WALK_ABOVE_M
      out[k] = this.surface(xy[2 * k], xy[2 * k + 1], top).z
    }
    return out
  }
}

/** Functional form of {@link WalkMesh.walkableHeight}, named after MeshWorldQuery.walkable_height. */
export function walkableHeight(mesh: WalkMesh, xy: ArrayLike<number>, navZ?: ArrayLike<number>): Float32Array {
  return mesh.walkableHeight(xy, navZ)
}
