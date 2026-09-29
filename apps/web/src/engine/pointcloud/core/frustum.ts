// LOD camera, frustum planes and AABB tests in the layer frame (M05 §6.4, §6.2.1; port of g02 lod.mjs makeCamera and
// classify). Owner: M05. Pure (no DOM, no clock); float64 throughout.
//
// Planes are six (a, b, c, d) rows, normalised by |(a, b, c)|, inside when a x + b y + c z + d >= 0, in the order
// left, right, bottom, top, near, far (the g02 prototype order). For a three.js camera the four side planes come from
// the rows of P x V x L (identical for standard and reversed depth, and they honour setViewOffset); near and far are
// built in view space (z_view <= -near, z_view >= -far) and carried to the layer frame, which avoids the reversed-Z
// depth row entirely.

export interface LodCamera {
  eye: Float64Array
  /** 6 planes x (a, b, c, d) in the layer frame */
  planes: Float64Array
  /** tan(fovY / 2) */
  slope: number
  /** raster height of the point pass, px (H_px = dbH x cloudScale) */
  hPx: number
  near: number
  far: number
  /** orthographic height in metres; 0 for a perspective camera (key = spacing x hPx / orthoH) */
  orthoH: number
  /** clip = C x p (column-major, layer frame), for NDC-based weights (download reordering, picking) */
  clip: Float64Array
}

export const OUTSIDE = 0
export const INTERSECT = 1
export const INSIDE = 2

export const newLodCamera = (): LodCamera => ({
  eye: new Float64Array(3), planes: new Float64Array(24), slope: 1, hPx: 1, near: 1, far: 20000, orthoH: 0, clip: new Float64Array(16),
})

export function copyLodCamera(src: LodCamera, out: LodCamera): LodCamera {
  out.eye.set(src.eye)
  out.planes.set(src.planes)
  out.clip.set(src.clip)
  out.slope = src.slope
  out.hPx = src.hPx
  out.near = src.near
  out.far = src.far
  out.orthoH = src.orthoH
  return out
}

/** 0 outside, 1 intersecting, 2 inside (the six planes) */
export function classify(pl: Float64Array, x0: number, y0: number, z0: number, x1: number, y1: number, z1: number): 0 | 1 | 2 {
  let inside = true
  for (let k = 0; k < 24; k += 4) {
    const a = pl[k]
    const b = pl[k + 1]
    const c = pl[k + 2]
    const d = pl[k + 3]
    const dmax = a * (a > 0 ? x1 : x0) + b * (b > 0 ? y1 : y0) + c * (c > 0 ? z1 : z0) + d
    if (dmax < 0) return 0
    const dmin = a * (a > 0 ? x0 : x1) + b * (b > 0 ? y0 : y1) + c * (c > 0 ? z0 : z1) + d
    if (dmin < 0) inside = false
  }
  return inside ? 2 : 1
}

/** classification of node i's tight box */
export function classifyNode(pl: Float64Array, tmin: Float64Array, tmax: Float64Array, i: number): 0 | 1 | 2 {
  const b = 3 * i
  return classify(pl, tmin[b], tmin[b + 1], tmin[b + 2], tmax[b], tmax[b + 1], tmax[b + 2])
}

/** distance from the eye to node i's tight box (0 inside) */
export function distToBox(tmin: Float64Array, tmax: Float64Array, i: number, e: Float64Array): number {
  const o = 3 * i
  const dx = Math.max(tmin[o] - e[0], 0, e[0] - tmax[o])
  const dy = Math.max(tmin[o + 1] - e[1], 0, e[1] - tmax[o + 1])
  const dz = Math.max(tmin[o + 2] - e[2], 0, e[2] - tmax[o + 2])
  return Math.hypot(dx, dy, dz)
}

const M = new Float64Array(16)
const A = new Float64Array(16)

function mul4(a: ArrayLike<number>, b: ArrayLike<number>, out: Float64Array): Float64Array {
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      let s = 0
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]
      out[c * 4 + r] = s
    }
  }
  return out
}

function setPlane(out: Float64Array, p: number, a: number, b: number, c: number, d: number): void {
  const l = Math.hypot(a, b, c) || 1
  out[4 * p] = a / l
  out[4 * p + 1] = b / l
  out[4 * p + 2] = c / l
  out[4 * p + 3] = d / l
}

/** view-space plane (a, b, c, d) carried to the layer frame through A = V x L (row vector times A) */
function viewPlane(out: Float64Array, p: number, a: number, b: number, c: number, d: number, AV: Float64Array): void {
  const x = a * AV[0] + b * AV[1] + c * AV[2] + d * AV[3]
  const y = a * AV[4] + b * AV[5] + c * AV[6] + d * AV[7]
  const z = a * AV[8] + b * AV[9] + c * AV[10] + d * AV[11]
  const w = a * AV[12] + b * AV[13] + c * AV[14] + d * AV[15]
  setPlane(out, p, x, y, z, w)
}

export interface ThreeCameraLike {
  projectionMatrix: { elements: ArrayLike<number> }
  matrixWorldInverse: { elements: ArrayLike<number> }
  matrixWorld: { elements: ArrayLike<number> }
  near: number
  far: number
  fov?: number
  isOrthographicCamera?: boolean
  top?: number
  bottom?: number
  zoom?: number
}

/**
 * LOD camera in the layer frame from a three.js camera: clip = P x V x L x p, with L the layer's matrixWorld (WorldRoot
 * rotation and T_world_layer) and Linv its inverse. hPx is the raster height of the point pass.
 */
export function makeLodCamera(cam: ThreeCameraLike, L: ArrayLike<number>, Linv: ArrayLike<number>, hPx: number, out: LodCamera): LodCamera {
  mul4(cam.matrixWorldInverse.elements, L, A)
  mul4(cam.projectionMatrix.elements, A, M)
  out.clip.set(M)
  for (let p = 0; p < 4; p++) {
    const s = p & 1 ? -1 : 1
    const ax = p < 2 ? 0 : 1
    setPlane(out.planes, p, M[3] + s * M[ax], M[7] + s * M[4 + ax], M[11] + s * M[8 + ax], M[15] + s * M[12 + ax])
  }
  viewPlane(out.planes, 4, 0, 0, -1, -cam.near, A)
  viewPlane(out.planes, 5, 0, 0, 1, cam.far, A)
  const w = cam.matrixWorld.elements
  const ex = w[12]
  const ey = w[13]
  const ez = w[14]
  out.eye[0] = Linv[0] * ex + Linv[4] * ey + Linv[8] * ez + Linv[12]
  out.eye[1] = Linv[1] * ex + Linv[5] * ey + Linv[9] * ez + Linv[13]
  out.eye[2] = Linv[2] * ex + Linv[6] * ey + Linv[10] * ez + Linv[14]
  out.near = cam.near
  out.far = cam.far
  out.hPx = hPx
  if (cam.isOrthographicCamera) {
    out.orthoH = ((cam.top ?? 1) - (cam.bottom ?? -1)) / (cam.zoom ?? 1)
    out.slope = 1
  } else {
    out.orthoH = 0
    out.slope = Math.tan(((cam.fov ?? 60) * Math.PI) / 360)
  }
  return out
}

/**
 * LOD camera from an eye and a target in the layer frame (ENU, Z up), same construction as g02 lod.mjs makeCamera
 * (OpenGL projection, up = +Z). Used by flight60 replays in Node, prefetch of a camera-flight destination and tests.
 */
export function lodCameraLookAt(eye: ArrayLike<number>, tgt: ArrayLike<number>, fovYDeg: number, W: number, H: number, near: number, far: number,
  hPx: number, out: LodCamera): LodCamera {
  let fx = tgt[0] - eye[0]
  let fy = tgt[1] - eye[1]
  let fz = tgt[2] - eye[2]
  const fl = Math.hypot(fx, fy, fz) || 1
  fx /= fl
  fy /= fl
  fz /= fl
  let rx = fy
  let ry = -fx
  let rz = 0
  let rl = Math.hypot(rx, ry, rz)
  if (rl < 1e-9) {
    rx = 1
    ry = 0
    rz = 0
    rl = 1
  }
  rx /= rl
  ry /= rl
  rz /= rl
  const ux = ry * fz - rz * fy
  const uy = rz * fx - rx * fz
  const uz = rx * fy - ry * fx
  const V = A
  V.fill(0)
  V[0] = rx
  V[4] = ry
  V[8] = rz
  V[12] = -(rx * eye[0] + ry * eye[1] + rz * eye[2])
  V[1] = ux
  V[5] = uy
  V[9] = uz
  V[13] = -(ux * eye[0] + uy * eye[1] + uz * eye[2])
  V[2] = -fx
  V[6] = -fy
  V[10] = -fz
  V[14] = fx * eye[0] + fy * eye[1] + fz * eye[2]
  V[15] = 1
  const slope = Math.tan((fovYDeg * Math.PI) / 360)
  const P = PT
  P.fill(0)
  P[0] = 1 / (slope * (W / H))
  P[5] = 1 / slope
  P[10] = -(far + near) / (far - near)
  P[11] = -1
  P[14] = (-2 * far * near) / (far - near)
  mul4(P, V, M)
  out.clip.set(M)
  const C = M
  for (let p = 0; p < 6; p++) {
    const s = p & 1 ? -1 : 1
    const ax = p >> 1
    setPlane(out.planes, p, C[3] + s * C[ax], C[7] + s * C[4 + ax], C[11] + s * C[8 + ax], C[15] + s * C[12 + ax])
  }
  out.eye[0] = eye[0]
  out.eye[1] = eye[1]
  out.eye[2] = eye[2]
  out.slope = slope
  out.hPx = hPx
  out.near = near
  out.far = far
  out.orthoH = 0
  return out
}
const PT = new Float64Array(16)

/** NDC xy of a layer-frame point through the camera's clip matrix; returns false behind the eye */
export function toNdc(cam: LodCamera, x: number, y: number, z: number, out: Float64Array): boolean {
  const C = cam.clip
  const w = C[3] * x + C[7] * y + C[11] * z + C[15]
  if (w <= 1e-9) return false
  out[0] = (C[0] * x + C[4] * y + C[8] * z + C[12]) / w
  out[1] = (C[1] * x + C[5] * y + C[9] * z + C[13]) / w
  return true
}
