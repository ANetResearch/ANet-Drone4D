// Route editor model (M15-FR-022 mission-edit; AWR-14 §5.3, §6.8; UX-FR-023, UX-FR-024; D1-AC-17): pure functions over the
// draft, so the store, the page and the tests share one implementation. A waypoint keeps its height in the reference the
// operator chose: AGL (height above the DTM at its E/N) or world z (ENU up). follow_path only takes world ENU points and
// one speed (AWR-17 §7.1); AGL points are converted with a terrain height (ground_dtm before submitting, the loaded DTM
// for drawing). Limits are ADR-016's: at most 1000 waypoints and 20 km per command; the draft history keeps 50 steps.
export type AltRef = 'AGL' | 'WORLD'
export interface DraftWp {
  /** stable id inside the draft (React keys, selection across edits) */
  id: number
  x: number
  y: number
  /** height in `ref`: metres above the terrain (AGL) or world z */
  h: number
  ref: AltRef
}
export type Ground = (x: number, y: number) => number

export const ROUTE_LIMITS = { maxWaypoints: 1000, maxLengthM: 20_000, historyMax: 50, minWaypoints: 2, nudgeM: 1, nudgeFastM: 10 } as const

let nextId = 1
/** a new waypoint id (monotonic for the page) */
export const newWpId = (): number => nextId++

export function worldZ(w: DraftWp, ground: Ground): number {
  return w.ref === 'WORLD' ? w.h : ground(w.x, w.y) + w.h
}

/** height of a world z expressed in a reference at (x, y) */
export function heightIn(ref: AltRef, x: number, y: number, z: number, ground: Ground): number {
  return ref === 'WORLD' ? z : z - ground(x, y)
}

/** xyz triples in world ENU (for drawing and for the coarse check) */
export function routePoints(wps: readonly DraftWp[], ground: Ground): Float64Array {
  const out = new Float64Array(wps.length * 3)
  wps.forEach((w, i) => {
    out[3 * i] = w.x
    out[3 * i + 1] = w.y
    out[3 * i + 2] = worldZ(w, ground)
  })
  return out
}

/** 3D length of the route in metres */
export function routeLength(pts: ArrayLike<number>): number {
  let L = 0
  for (let i = 3; i + 2 < pts.length; i += 3) L += Math.hypot(pts[i] - pts[i - 3], pts[i + 1] - pts[i - 2], pts[i + 2] - pts[i - 1])
  return L
}

export const canAdd = (n: number): boolean => n < ROUTE_LIMITS.maxWaypoints

/** insert at index i (0..n); the new point is a copy of `wp` with a fresh id */
export function insertAt(wps: readonly DraftWp[], i: number, wp: Omit<DraftWp, 'id'>): DraftWp[] {
  const k = Math.max(0, Math.min(wps.length, i))
  return [...wps.slice(0, k), { ...wp, id: newWpId() }, ...wps.slice(k)]
}

export function removeAt(wps: readonly DraftWp[], i: number): DraftWp[] {
  return i < 0 || i >= wps.length ? [...wps] : [...wps.slice(0, i), ...wps.slice(i + 1)]
}

export function updateAt(wps: readonly DraftWp[], i: number, patch: Partial<Omit<DraftWp, 'id'>>): DraftWp[] {
  if (i < 0 || i >= wps.length) return [...wps]
  const out = [...wps]
  out[i] = { ...out[i], ...patch }
  return out
}

/** swap waypoint i with its neighbour (dir -1 up, +1 down); unchanged at the ends */
export function reorder(wps: readonly DraftWp[], i: number, dir: -1 | 1): DraftWp[] {
  const j = i + dir
  if (i < 0 || i >= wps.length || j < 0 || j >= wps.length) return [...wps]
  const out = [...wps]
  ;[out[i], out[j]] = [out[j], out[i]]
  return out
}

/** change a waypoint's reference keeping its world height */
export function withRef(w: DraftWp, ref: AltRef, ground: Ground): DraftWp {
  if (w.ref === ref) return w
  return { ...w, ref, h: heightIn(ref, w.x, w.y, worldZ(w, ground), ground) }
}

/** the midpoint of segment k (between waypoints k and k + 1), keeping the reference of waypoint k */
export function midpoint(wps: readonly DraftWp[], k: number, ground: Ground): Omit<DraftWp, 'id'> | null {
  const a = wps[k]
  const b = wps[k + 1]
  if (!a || !b) return null
  const x = (a.x + b.x) / 2
  const y = (a.y + b.y) / 2
  const z = (worldZ(a, ground) + worldZ(b, ground)) / 2
  return { x, y, ref: a.ref, h: heightIn(a.ref, x, y, z, ground) }
}

/** bounded undo history: `past` ends with the state before the current one */
export interface History { past: (readonly DraftWp[])[]; future: (readonly DraftWp[])[] }
export function pushHistory(h: History, before: readonly DraftWp[]): History {
  const past = [...h.past, before]
  while (past.length > ROUTE_LIMITS.historyMax) past.shift()
  return { past, future: [] }
}
export function undo(h: History, cur: readonly DraftWp[]): { h: History; wps: readonly DraftWp[] } | null {
  if (!h.past.length) return null
  const wps = h.past[h.past.length - 1]
  return { h: { past: h.past.slice(0, -1), future: [cur, ...h.future] }, wps }
}
export function redo(h: History, cur: readonly DraftWp[]): { h: History; wps: readonly DraftWp[] } | null {
  if (!h.future.length) return null
  const [wps, ...rest] = h.future
  return { h: { past: [...h.past, cur], future: rest }, wps }
}

/** same route (ids ignored): the CLEAN test after undo */
export function sameRoute(a: readonly DraftWp[], b: readonly DraftWp[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    const p = a[i]
    const q = b[i]
    if (p.x !== q.x || p.y !== q.y || p.h !== q.h || p.ref !== q.ref) return false
  }
  return true
}

// ------------------------------------------------------------------------------------------------ area (UX-FR-024)
export type AreaPts = readonly (readonly [number, number])[]

/** axis-aligned rectangle from two corners (counter-clockwise from the lower left) */
export function rectFrom(a: readonly [number, number], b: readonly [number, number]): [number, number][] {
  const x0 = Math.min(a[0], b[0])
  const x1 = Math.max(a[0], b[0])
  const y0 = Math.min(a[1], b[1])
  const y1 = Math.max(a[1], b[1])
  return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
}

/** signed area (counter-clockwise positive) */
export function polygonArea(p: AreaPts): number {
  let s = 0
  for (let i = 0; i < p.length; i++) {
    const a = p[i]
    const b = p[(i + 1) % p.length]
    s += a[0] * b[1] - b[0] * a[1]
  }
  return s / 2
}

const cross = (o: readonly number[], a: readonly number[], b: readonly number[]): number => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
function segsCross(p1: readonly number[], p2: readonly number[], p3: readonly number[], p4: readonly number[]): boolean {
  const d1 = cross(p3, p4, p1)
  const d2 = cross(p3, p4, p2)
  const d3 = cross(p1, p2, p3)
  const d4 = cross(p1, p2, p4)
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))
}

/** a closed ring whose non-adjacent edges cross (refused when the area is closed, AWR-14 §6.8) */
export function selfIntersects(p: AreaPts): boolean {
  const n = p.length
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (Math.abs(i - j) <= 1 || (i === 0 && j === n - 1)) continue
      if (segsCross(p[i], p[(i + 1) % n], p[j], p[(j + 1) % n])) return true
    }
  }
  return false
}

export function centroid(p: AreaPts): [number, number] {
  let x = 0
  let y = 0
  for (const q of p) {
    x += q[0]
    y += q[1]
  }
  return p.length ? [x / p.length, y / p.length] : [0, 0]
}

/** half the bounding box diagonal: the search extent of an expanding square started at the centroid */
export function halfExtent(p: AreaPts): number {
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  for (const q of p) {
    x0 = Math.min(x0, q[0])
    x1 = Math.max(x1, q[0])
    y0 = Math.min(y0, q[1])
    y1 = Math.max(y1, q[1])
  }
  return p.length ? Math.max(x1 - x0, y1 - y0) / 2 : 0
}

export type AreaGenerator = 'lawnmower' | 'expanding_square'
export interface AreaParams { generator: AreaGenerator; aglM: number; spacingM: number; speedMps: number }

/** M10 generator parameters of a closed area (packages/contracts scenario.schema.json gen_lawnmower, gen_expanding_square) */
export function generatorParams(area: AreaPts, p: AreaParams): Record<string, unknown> {
  if (p.generator === 'lawnmower') {
    const ring = polygonArea(area) < 0 ? [...area].reverse() : [...area]
    return {
      polygon_enu_m: ring.map((q) => [round2(q[0]), round2(q[1])]), altitude: { mode: 'fixed_agl', agl_m: p.aglM },
      spacing_m: p.spacingM, speed_mps: p.speedMps,
    }
  }
  const c = centroid(area)
  return {
    datum_enu_m: [round2(c[0]), round2(c[1])], agl_m: p.aglM, leg0_m: p.spacingM, max_extent_m: Math.max(p.spacingM, round2(halfExtent(area))),
    speed_mps: p.speedMps,
  }
}
const round2 = (v: number): number => Math.round(v * 100) / 100

// ------------------------------------------------------------------------------------------------ hit tests (viewport)
/** index of the waypoint whose screen point is nearest to (x, y) within tolPx, or -1 */
export function nearestScreen(screen: ArrayLike<number>, n: number, x: number, y: number, tolPx: number): number {
  let best = -1
  let bd = tolPx * tolPx
  for (let i = 0; i < n; i++) {
    const sx = screen[2 * i]
    const sy = screen[2 * i + 1]
    if (!Number.isFinite(sx)) continue
    const d = (sx - x) ** 2 + (sy - y) ** 2
    if (d <= bd) {
      bd = d
      best = i
    }
  }
  return best
}

/** segment k whose screen midpoint is within tolPx of (x, y) and which is long enough to insert into, or -1 */
export function nearestMidpoint(screen: ArrayLike<number>, n: number, x: number, y: number, tolPx: number, minLenPx = 24): number {
  let best = -1
  let bd = tolPx * tolPx
  for (let k = 0; k + 1 < n; k++) {
    const ax = screen[2 * k]
    const ay = screen[2 * k + 1]
    const bx = screen[2 * k + 2]
    const by = screen[2 * k + 3]
    if (!Number.isFinite(ax) || !Number.isFinite(bx) || Math.hypot(bx - ax, by - ay) < minLenPx) continue
    const d = ((ax + bx) / 2 - x) ** 2 + ((ay + by) / 2 - y) ** 2
    if (d <= bd) {
      bd = d
      best = k
    }
  }
  return best
}

/** intersection of an ENU ray with the horizontal plane z = z0, null when parallel or behind */
export function rayPlaneZ(o: ArrayLike<number>, d: ArrayLike<number>, z0: number): [number, number] | null {
  if (Math.abs(d[2]) < 1e-6) return null
  const t = (z0 - o[2]) / d[2]
  if (!(t > 0)) return null
  return [o[0] + t * d[0], o[1] + t * d[1]]
}

/**
 * height of the point of an ENU ray closest to the vertical line through (x, y): Shift + drag changes only the height of
 * a waypoint, following the pointer along that line
 */
export function rayVerticalZ(o: ArrayLike<number>, d: ArrayLike<number>, x: number, y: number): number | null {
  // closest points between the ray o + t d and the line (x, y, 0) + s (0, 0, 1)
  const wx = o[0] - x
  const wy = o[1] - y
  const a = d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
  const b = d[2]
  const dd = d[0] * wx + d[1] * wy + d[2] * o[2]
  const e = o[2]
  const den = a - b * b
  if (den < 1e-9) return null
  const t = (b * e - dd) / den
  if (!(t > 0)) return null
  return o[2] + t * d[2]
}
