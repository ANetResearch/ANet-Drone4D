// Camera intrinsics, projection, sensor frame rect, T_base_cam and frustum corners (M13-FR-020, FR-023; M13 §6.5.12, §7.4;
// AWR-03 §5.1 rule 7). Owner: M13. The single place where the FPV camera intrinsics and the FLU -> optical / three
// camera constants live on the web side (AWR-03 §4.3); M06 draws the frustum and sets the FPV camera from these values.
// Conventions: sensor frame FLU (x = optical axis); optical frame RDF; three camera RUB (looks -Z). Pixels follow COLMAP
// (pixel centre 0.5): u = fx·x_opt/z_opt + cx, v = fy·y_opt/z_opt + cy. Gimbal R = Rz(az)·Ry(-el) (el up positive, az left
// positive). All outputs go to caller-preallocated Float64Arrays; nothing here allocates or imports render code.
// Matrix outputs are column-major (three Matrix4.fromArray); the 3x3 constants and SensorView.mount are row-major.

/** the subset of a SensorView the geometry needs */
export interface SensorGeom {
  w: number
  h: number
  fx: number
  fy: number
  cx: number
  cy: number
  /** 16, row-major T_base_mount */
  mount: Float64Array
  hasGimbal: boolean
  /** smoothed gimbal angles (rad) */
  gimbal: { az: number; el: number }
}

/** FLU -> three camera RUB, 3x3 row-major [[0,0,-1],[-1,0,0],[0,1,0]] */
export const R_FLU_CAM: Float64Array = Float64Array.of(0, 0, -1, -1, 0, 0, 0, 1, 0)
/** FLU -> optical RDF, 3x3 row-major [[0,0,1],[-1,0,0],[0,-1,0]] */
export const R_FLU_OPT: Float64Array = Float64Array.of(0, 0, 1, -1, 0, 0, 0, -1, 0)

const m9a = new Float64Array(9)
const m9b = new Float64Array(9)

/** Rz(az)·Ry(-el), 3x3 row-major */
export function gimbalR(az: number, el: number, out: Float64Array): Float64Array {
  const ca = Math.cos(az)
  const sa = Math.sin(az)
  const ce = Math.cos(el)
  const se = Math.sin(el)
  out[0] = ca * ce
  out[1] = -sa
  out[2] = -ca * se
  out[3] = sa * ce
  out[4] = ca
  out[5] = -sa * se
  out[6] = se
  out[7] = 0
  out[8] = ce
  return out
}

/** row-major 3x3 product a·b into out (out may not alias a or b) */
export function mul3(a: ArrayLike<number>, b: ArrayLike<number>, out: Float64Array): Float64Array {
  for (let i = 0; i < 3; i++) {
    const a0 = a[3 * i]
    const a1 = a[3 * i + 1]
    const a2 = a[3 * i + 2]
    out[3 * i] = a0 * b[0] + a1 * b[3] + a2 * b[6]
    out[3 * i + 1] = a0 * b[1] + a1 * b[4] + a2 * b[7]
    out[3 * i + 2] = a0 * b[2] + a1 * b[5] + a2 * b[8]
  }
  return out
}

/** rotation part of the row-major 4x4 mount into a row-major 3x3 */
function mountRot(m: Float64Array, out: Float64Array): Float64Array {
  out[0] = m[0]
  out[1] = m[1]
  out[2] = m[2]
  out[3] = m[4]
  out[4] = m[5]
  out[5] = m[6]
  out[6] = m[8]
  out[7] = m[9]
  out[8] = m[10]
  return out
}

/** R_base_sensor = R_mount·R_gimbal (row-major 3x3) */
export function baseSensorR(s: SensorGeom, out: Float64Array): Float64Array {
  mountRot(s.mount, m9a)
  if (!s.hasGimbal) {
    for (let i = 0; i < 9; i++) out[i] = m9a[i]
    return out
  }
  gimbalR(s.gimbal.az, s.gimbal.el, m9b)
  return mul3(m9a, m9b, out)
}

/**
 * Projection with the principal point, contain rule (M13 §6.5.12): a wider viewport keeps the vertical FOV and extends
 * horizontally, a narrower one keeps the horizontal FOV and extends vertically. Column-major 4x4 into out.
 */
export function projectionFor(s: SensorGeom, aspect: number, near: number, far: number, out: Float64Array): Float64Array {
  const as = s.w / s.h
  let sx = (2 * s.fx) / s.w
  let sy = (2 * s.fy) / s.h
  let ox = 1 - (2 * s.cx) / s.w
  let oy = (2 * s.cy) / s.h - 1
  if (aspect > as) {
    const k = as / aspect
    sx *= k
    ox *= k
  } else if (aspect < as) {
    const k = aspect / as
    sy *= k
    oy *= k
  }
  const A = -(far + near) / (far - near)
  const B = (-2 * far * near) / (far - near)
  // row-major P = [[sx,0,ox,0],[0,sy,oy,0],[0,0,A,B],[0,0,-1,0]] written column-major
  out[0] = sx
  out[1] = 0
  out[2] = 0
  out[3] = 0
  out[4] = 0
  out[5] = sy
  out[6] = 0
  out[7] = 0
  out[8] = ox
  out[9] = oy
  out[10] = A
  out[11] = -1
  out[12] = 0
  out[13] = 0
  out[14] = B
  out[15] = 0
  return out
}

/** NDC rectangle of the true sensor image inside the viewport: x0, y0, x1, y1 (M13-FR-023) */
export function frameRect(s: SensorGeom, aspect: number, out: Float64Array): Float64Array {
  const as = s.w / s.h
  let kx = 1
  let ky = 1
  if (aspect > as) kx = as / aspect
  else if (aspect < as) ky = aspect / as
  out[0] = -kx
  out[1] = -ky
  out[2] = kx
  out[3] = ky
  return out
}

const r9 = new Float64Array(9)
const c9 = new Float64Array(9)

/** T_base_cam = T_base_mount·R_gimbal·R_FLU_CAM (three camera in the body frame), column-major 4x4 into out */
export function T_base_cam(s: SensorGeom, out: Float64Array): Float64Array {
  baseSensorR(s, r9)
  mul3(r9, R_FLU_CAM, c9)
  const m = s.mount
  out[0] = c9[0]
  out[1] = c9[3]
  out[2] = c9[6]
  out[3] = 0
  out[4] = c9[1]
  out[5] = c9[4]
  out[6] = c9[7]
  out[7] = 0
  out[8] = c9[2]
  out[9] = c9[5]
  out[10] = c9[8]
  out[11] = 0
  out[12] = m[3]
  out[13] = m[7]
  out[14] = m[11]
  out[15] = 1
  return out
}

/**
 * Frustum points in the body frame (M13 §6.5.12): origin (the mount point) then the far corners of pixels (0,0), (w,0),
 * (w,h), (0,h) at depth L along the optical axis: (L, -(u-cx)·L/fx, -(v-cy)·L/fy) in the sensor frame, times
 * T_base_mount·R_gimbal (smoothed angles). 15 values into out. Same principal point as projectionFor.
 */
export function frustumCorners(s: SensorGeom, L: number, out: Float64Array): Float64Array {
  baseSensorR(s, r9)
  const m = s.mount
  const tx = m[3]
  const ty = m[7]
  const tz = m[11]
  out[0] = tx
  out[1] = ty
  out[2] = tz
  for (let k = 0; k < 4; k++) {
    const u = k === 1 || k === 2 ? s.w : 0
    const v = k >= 2 ? s.h : 0
    const x = L
    const y = (-(u - s.cx) * L) / s.fx
    const z = (-(v - s.cy) * L) / s.fy
    const o = 3 + 3 * k
    out[o] = tx + r9[0] * x + r9[1] * y + r9[2] * z
    out[o + 1] = ty + r9[3] * x + r9[4] * y + r9[5] * z
    out[o + 2] = tz + r9[6] * x + r9[7] * y + r9[8] * z
  }
  return out
}

/** pixel of a point given in the sensor FLU frame; NaN when behind the camera. out: [u, v] */
export function pixelOfSensorPoint(s: SensorGeom, x: number, y: number, z: number, out: Float64Array): Float64Array {
  if (!(x > 0)) {
    out[0] = Number.NaN
    out[1] = Number.NaN
    return out
  }
  out[0] = (s.fx * -y) / x + s.cx
  out[1] = (s.fy * -z) / x + s.cy
  return out
}

/** HFOV and VFOV (rad) from the intrinsics; with an asymmetric principal point the two sides are summed */
export function fovOf(s: Pick<SensorGeom, 'w' | 'h' | 'fx' | 'fy' | 'cx' | 'cy'>, out: Float64Array): Float64Array {
  out[0] = Math.atan(s.cx / s.fx) + Math.atan((s.w - s.cx) / s.fx)
  out[1] = Math.atan(s.cy / s.fy) + Math.atan((s.h - s.cy) / s.fy)
  return out
}

/** row-major 4x4 T_base_mount from a translation and a ZYX rotation Rz(yaw)·Ry(pitch)·Rx(roll) (degrees) */
export function mountMatrix(xyz: ArrayLike<number>, rpyDeg: ArrayLike<number>, out: Float64Array): Float64Array {
  const d = Math.PI / 180
  const cr = Math.cos(rpyDeg[0] * d)
  const sr = Math.sin(rpyDeg[0] * d)
  const cp = Math.cos(rpyDeg[1] * d)
  const sp = Math.sin(rpyDeg[1] * d)
  const cy = Math.cos(rpyDeg[2] * d)
  const sy = Math.sin(rpyDeg[2] * d)
  out[0] = cy * cp
  out[1] = cy * sp * sr - sy * cr
  out[2] = cy * sp * cr + sy * sr
  out[3] = xyz[0]
  out[4] = sy * cp
  out[5] = sy * sp * sr + cy * cr
  out[6] = sy * sp * cr - cy * sr
  out[7] = xyz[1]
  out[8] = -sp
  out[9] = cp * sr
  out[10] = cp * cr
  out[11] = xyz[2]
  out[12] = 0
  out[13] = 0
  out[14] = 0
  out[15] = 1
  return out
}
