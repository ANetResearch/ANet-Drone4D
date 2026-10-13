// Sensitivity volume meshes (M18-FR-024; M18-M19 PRD §8.2: sector 48 segments, sphere and hemisphere 24 x 12). Owner: M18.
// bodyMesh() appends the translucent faces (triangle list, local ENU metres around the target foot point, yaw applied)
// and the outline (segments) of one body to MeshBuffers. The overlay builds the local mesh once per configuration and
// only translates it when the target moves (no allocation per frame). Moving targets plan sectors as full circles.
// bodyFaces() (D2-FX-R2-web round 2, D2-AC-30) gives the same triangles as an indexed mesh: shared vertices (sector
// 2 x 49 + 2, hemisphere 25 x 7, sphere 25 x 13) instead of 6 per quad, so the overlay transforms and re-uploads about a
// fifth of the face vertices when the targets move (the index list only changes with the set of bodies).
import { type SensitivityBody } from './geometry'

export const SECTOR_SEGS = 48
export const SPHERE_LON = 24
export const SPHERE_LAT = 12

export interface MeshBuffers {
  /** triangle vertices, 3 floats each */
  tri: Float32Array
  nTri: number
  /** outline segments, 6 floats each */
  seg: Float32Array
  nSeg: number
}

export function newMeshBuffers(triVerts = 4096, segs = 1024): MeshBuffers {
  return { tri: new Float32Array(triVerts * 3), nTri: 0, seg: new Float32Array(segs * 6), nSeg: 0 }
}

function growTri(out: MeshBuffers, add: number): void {
  if ((out.nTri + add) * 3 <= out.tri.length) return
  const t = new Float32Array(Math.max(out.tri.length * 2, (out.nTri + add) * 3))
  t.set(out.tri.subarray(0, out.nTri * 3))
  out.tri = t
}
function growSeg(out: MeshBuffers, add: number): void {
  if ((out.nSeg + add) * 6 <= out.seg.length) return
  const s = new Float32Array(Math.max(out.seg.length * 2, (out.nSeg + add) * 6))
  s.set(out.seg.subarray(0, out.nSeg * 6))
  out.seg = s
}
function v(out: MeshBuffers, x: number, y: number, z: number): void {
  const o = out.nTri * 3
  out.tri[o] = x
  out.tri[o + 1] = y
  out.tri[o + 2] = z
  out.nTri++
}
function quad(out: MeshBuffers, a: number[], b: number[], c: number[], d: number[]): void {
  v(out, a[0], a[1], a[2]); v(out, b[0], b[1], b[2]); v(out, c[0], c[1], c[2])
  v(out, a[0], a[1], a[2]); v(out, c[0], c[1], c[2]); v(out, d[0], d[1], d[2])
}
function line(out: MeshBuffers, a: number[], b: number[]): void {
  const o = out.nSeg * 6
  out.seg[o] = a[0]; out.seg[o + 1] = a[1]; out.seg[o + 2] = a[2]
  out.seg[o + 3] = b[0]; out.seg[o + 4] = b[1]; out.seg[o + 5] = b[2]
  out.nSeg++
}

/** append the faces and outline of one body (local frame: origin at the target foot point, ENU axes) */
export function bodyMesh(body: SensitivityBody, yawRad: number, movingAsCircle: boolean, out: MeshBuffers, radiusM?: number): void {
  const r = radiusM ?? body.radius_max_m
  let shape = body.shape
  const half = ((body.half_angle_deg ?? 60) * Math.PI) / 180
  if (shape === 'sector' && (movingAsCircle || half >= Math.PI - 1e-9)) shape = 'cylinder'
  if (shape === 'sphere' || shape === 'hemisphere') {
    const lat0 = shape === 'hemisphere' ? 0 : -Math.PI / 2
    const nLat = shape === 'hemisphere' ? SPHERE_LAT / 2 : SPHERE_LAT
    const P = (i: number, j: number): number[] => {
      const lon = (i / SPHERE_LON) * 2 * Math.PI
      const lat = lat0 + (j / nLat) * (Math.PI / 2 - lat0)
      return [r * Math.cos(lat) * Math.cos(lon), r * Math.cos(lat) * Math.sin(lon), r * Math.sin(lat)]
    }
    growTri(out, SPHERE_LON * nLat * 6)
    for (let j = 0; j < nLat; j++) for (let i = 0; i < SPHERE_LON; i++) quad(out, P(i, j), P(i + 1, j), P(i + 1, j + 1), P(i, j + 1))
    growSeg(out, SPHERE_LON * 2 + nLat * 2)
    const jEq = shape === 'hemisphere' ? 0 : nLat / 2
    for (let i = 0; i < SPHERE_LON; i++) line(out, P(i, jEq), P(i + 1, jEq))
    for (const i of [0, SPHERE_LON / 4, SPHERE_LON / 2, (3 * SPHERE_LON) / 4]) for (let j = 0; j < nLat; j++) line(out, P(i, j), P(i, j + 1))
    return
  }
  const z0 = body.z_min_m ?? 0
  const z1 = body.z_max_m ?? 300
  const full = shape === 'cylinder'
  const a0 = full ? 0 : yawRad + ((body.azimuth_offset_deg ?? 0) * Math.PI) / 180 - half
  const span = full ? 2 * Math.PI : 2 * half
  const n = SECTOR_SEGS
  const A = (k: number, z: number): number[] => {
    const a = a0 + (k / n) * span
    return [r * Math.cos(a), r * Math.sin(a), z]
  }
  const C0 = [0, 0, z0]
  const C1 = [0, 0, z1]
  growTri(out, n * 12 + 12)
  for (let k = 0; k < n; k++) {
    quad(out, A(k, z0), A(k + 1, z0), A(k + 1, z1), A(k, z1))
    const t0 = A(k, z0), t1 = A(k + 1, z0), u0 = A(k, z1), u1 = A(k + 1, z1)
    v(out, C0[0], C0[1], C0[2]); v(out, t1[0], t1[1], t1[2]); v(out, t0[0], t0[1], t0[2])
    v(out, C1[0], C1[1], C1[2]); v(out, u0[0], u0[1], u0[2]); v(out, u1[0], u1[1], u1[2])
  }
  if (!full) {
    quad(out, C0, A(0, z0), A(0, z1), C1)
    quad(out, C0, C1, A(n, z1), A(n, z0))
  }
  growSeg(out, n * 2 + 6)
  for (let k = 0; k < n; k++) {
    line(out, A(k, z0), A(k + 1, z0))
    line(out, A(k, z1), A(k + 1, z1))
  }
  if (!full) {
    line(out, C0, A(0, z0)); line(out, C1, A(0, z1)); line(out, C0, A(n, z0)); line(out, C1, A(n, z1))
    line(out, A(0, z0), A(0, z1)); line(out, A(n, z0), A(n, z1))
  } else {
    for (const k of [0, n / 4, n / 2, (3 * n) / 4]) line(out, A(k, z0), A(k, z1))
  }
}

/** an indexed triangle mesh in local metres (vertices 3 floats each, triangles as vertex indices) */
export interface IndexedFaces {
  pos: Float32Array
  nVert: number
  idx: Uint32Array
  nIdx: number
}

/** the faces of bodyMesh() as an indexed mesh: same triangles (same order and winding), shared vertices */
export function bodyFaces(body: SensitivityBody, yawRad: number, movingAsCircle: boolean, radiusM?: number): IndexedFaces {
  const r = radiusM ?? body.radius_max_m
  let shape = body.shape
  const half = ((body.half_angle_deg ?? 60) * Math.PI) / 180
  if (shape === 'sector' && (movingAsCircle || half >= Math.PI - 1e-9)) shape = 'cylinder'
  if (shape === 'sphere' || shape === 'hemisphere') {
    const lat0 = shape === 'hemisphere' ? 0 : -Math.PI / 2
    const nLat = shape === 'hemisphere' ? SPHERE_LAT / 2 : SPHERE_LAT
    const W = SPHERE_LON + 1
    const pos = new Float32Array(W * (nLat + 1) * 3)
    for (let j = 0; j <= nLat; j++) {
      for (let i = 0; i <= SPHERE_LON; i++) {
        const lon = (i / SPHERE_LON) * 2 * Math.PI
        const lat = lat0 + (j / nLat) * (Math.PI / 2 - lat0)
        const o = 3 * (j * W + i)
        pos[o] = r * Math.cos(lat) * Math.cos(lon)
        pos[o + 1] = r * Math.cos(lat) * Math.sin(lon)
        pos[o + 2] = r * Math.sin(lat)
      }
    }
    const idx = new Uint32Array(SPHERE_LON * nLat * 6)
    let n = 0
    for (let j = 0; j < nLat; j++) {
      for (let i = 0; i < SPHERE_LON; i++) {
        const a = j * W + i, b = j * W + i + 1, c = (j + 1) * W + i + 1, d = (j + 1) * W + i
        idx[n++] = a; idx[n++] = b; idx[n++] = c
        idx[n++] = a; idx[n++] = c; idx[n++] = d
      }
    }
    return { pos, nVert: W * (nLat + 1), idx, nIdx: n }
  }
  const z0 = body.z_min_m ?? 0
  const z1 = body.z_max_m ?? 300
  const full = shape === 'cylinder'
  const a0 = full ? 0 : yawRad + ((body.azimuth_offset_deg ?? 0) * Math.PI) / 180 - half
  const span = full ? 2 * Math.PI : 2 * half
  const n = SECTOR_SEGS
  // vertices: arc at z0 (0..n), arc at z1 (n+1..2n+1), C0, C1
  const nVert = 2 * (n + 1) + 2
  const pos = new Float32Array(nVert * 3)
  for (let k = 0; k <= n; k++) {
    const a = a0 + (k / n) * span
    const x = r * Math.cos(a), y = r * Math.sin(a)
    pos[3 * k] = x; pos[3 * k + 1] = y; pos[3 * k + 2] = z0
    const u = 3 * (n + 1 + k)
    pos[u] = x; pos[u + 1] = y; pos[u + 2] = z1
  }
  const C0 = 2 * (n + 1), C1 = C0 + 1
  pos[3 * C0 + 2] = z0
  pos[3 * C1 + 2] = z1
  const lo = (k: number) => k
  const hi = (k: number) => n + 1 + k
  const idx = new Uint32Array(n * 12 + (full ? 0 : 12))
  let m = 0
  const quad = (a: number, b: number, c: number, d: number): void => {
    idx[m++] = a; idx[m++] = b; idx[m++] = c
    idx[m++] = a; idx[m++] = c; idx[m++] = d
  }
  for (let k = 0; k < n; k++) {
    quad(lo(k), lo(k + 1), hi(k + 1), hi(k))
    idx[m++] = C0; idx[m++] = lo(k + 1); idx[m++] = lo(k)
    idx[m++] = C1; idx[m++] = hi(k); idx[m++] = hi(k + 1)
  }
  if (!full) {
    quad(C0, lo(0), hi(0), C1)
    quad(C0, C1, hi(n), lo(n))
  }
  return { pos, nVert, idx, nIdx: m }
}

/** box wireframe of a target (l along the heading, w across, h up; origin at the foot point): 12 edges + heading tick */
export function boxEdges(l: number, w: number, h: number, yawRad: number, out: MeshBuffers): void {
  const c = Math.cos(yawRad), s = Math.sin(yawRad)
  const P = (x: number, y: number, z: number): number[] => [c * x - s * y, s * x + c * y, z]
  const xs = [-l / 2, l / 2], ys = [-w / 2, w / 2], zs = [0, h]
  growSeg(out, 13)
  for (const y of ys) for (const z of zs) line(out, P(xs[0], y, z), P(xs[1], y, z))
  for (const x of xs) for (const z of zs) line(out, P(x, ys[0], z), P(x, ys[1], z))
  for (const x of xs) for (const y of ys) line(out, P(x, y, zs[0]), P(x, y, zs[1]))
  const tick = Math.max(1.5, l)
  line(out, P(l / 2, 0, h / 2), P(l / 2 + tick, 0, h / 2))
}
