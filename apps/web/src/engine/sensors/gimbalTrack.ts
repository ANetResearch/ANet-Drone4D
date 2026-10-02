import { hypot4 } from '../hypot'
// Gimbal visual follow (M13-FR-021; M13 §6.5.12; ADR-046; P-04). Owner: M13. A SensorPose48 sample carries the world
// orientation of the sensor q_ws (WORLD<-SENSOR, FLU); with the body orientation q_wb interpolated at the same sample time
// the gimbal angles follow as R_g = R_mountᵀ·R(conj(q_wb)·q_ws), az = atan2(R_g[1][0], R_g[0][0]), el = asin(R_g[2][0])
// (the first column of Rz(az)·Ry(-el) is the optical axis). The drawn angles approach the target with a critically damped
// second-order response, tau = 0.15 s, integrated exactly per frame (stable for any dt); the reduced motion tier jumps to
// the target. The server stays authoritative (P-04): the damping only shapes the picture. No allocation.

export const GIMBAL_TAU_S = 0.15

export interface GimbalState {
  az: number
  el: number
  azT: number
  elT: number
  vaz: number
  vel: number
}

/** a·b for [x, y, z, w] quaternions (Hamilton), out may alias neither input */
export function quatMul(a: ArrayLike<number>, b: ArrayLike<number>, out: Float64Array, o = 0): Float64Array {
  const ax = a[0]
  const ay = a[1]
  const az = a[2]
  const aw = a[3]
  const bx = b[0]
  const by = b[1]
  const bz = b[2]
  const bw = b[3]
  out[o] = aw * bx + ax * bw + ay * bz - az * by
  out[o + 1] = aw * by - ax * bz + ay * bw + az * bx
  out[o + 2] = aw * bz + ax * by - ay * bx + az * bw
  out[o + 3] = aw * bw - ax * bx - ay * by - az * bz
  return out
}

/** rotation matrix (row-major 3x3) of a unit [x, y, z, w] quaternion */
export function quatToR(q: ArrayLike<number>, out: Float64Array, o = 0): Float64Array {
  const x = q[o]
  const y = q[o + 1]
  const z = q[o + 2]
  const w = q[o + 3]
  out[0] = 1 - 2 * (y * y + z * z)
  out[1] = 2 * (x * y - w * z)
  out[2] = 2 * (x * z + w * y)
  out[3] = 2 * (x * y + w * z)
  out[4] = 1 - 2 * (x * x + z * z)
  out[5] = 2 * (y * z - w * x)
  out[6] = 2 * (x * z - w * y)
  out[7] = 2 * (y * z + w * x)
  out[8] = 1 - 2 * (x * x + y * y)
  return out
}

/** normalised lerp between two [x, y, z, w] quaternions at t (hemisphere aligned first), into out */
export function nlerp(a: ArrayLike<number>, ao: number, b: ArrayLike<number>, bo: number, t: number, out: Float64Array): Float64Array {
  let dot = a[ao] * b[bo] + a[ao + 1] * b[bo + 1] + a[ao + 2] * b[bo + 2] + a[ao + 3] * b[bo + 3]
  const sgn = dot < 0 ? -1 : 1
  dot = 0
  for (let i = 0; i < 4; i++) {
    out[i] = (1 - t) * a[ao + i] + t * sgn * b[bo + i]
    dot += out[i] * out[i]
  }
  const n = Math.sqrt(dot) || 1
  for (let i = 0; i < 4; i++) out[i] /= n
  return out
}

const qInv = new Float64Array(4)
const qBS = new Float64Array(4)
const rBS = new Float64Array(9)

/**
 * Gimbal target angles from one sample: q_ws (sensor, world), q_wb (body at the sample time), mount (row-major 4x4).
 * out: [azT, elT]. Returns false when the inputs are not finite.
 */
export function gimbalFromSample(qWS: ArrayLike<number>, qWB: ArrayLike<number>, mount: Float64Array, out: Float64Array): boolean {
  qInv[0] = -qWB[0]
  qInv[1] = -qWB[1]
  qInv[2] = -qWB[2]
  qInv[3] = qWB[3]
  quatMul(qInv, qWS, qBS)
  const n = hypot4(qBS[0], qBS[1], qBS[2], qBS[3])
  if (!(n > 0) || !Number.isFinite(n)) return false
  for (let i = 0; i < 4; i++) qBS[i] /= n
  quatToR(qBS, rBS)
  // first column of R_g = R_mountᵀ·R_bs
  const c0 = mount[0] * rBS[0] + mount[4] * rBS[3] + mount[8] * rBS[6]
  const c1 = mount[1] * rBS[0] + mount[5] * rBS[3] + mount[9] * rBS[6]
  const c2 = mount[2] * rBS[0] + mount[6] * rBS[3] + mount[10] * rBS[6]
  out[0] = Math.atan2(c1, c0)
  out[1] = Math.asin(Math.max(-1, Math.min(1, c2)))
  return true
}

/**
 * Critically damped step towards the targets over dtS (exact solution of x'' = -2ωx' - ω²(x - xT), ω = 1/τ).
 * `reduced` jumps to the target (motion tier reduced).
 */
export function dampGimbal(g: GimbalState, dtS: number, reduced: boolean, tauS = GIMBAL_TAU_S): void {
  if (reduced || !(dtS > 0)) {
    if (reduced) {
      g.az = g.azT
      g.el = g.elT
      g.vaz = 0
      g.vel = 0
    }
    return
  }
  const w = 1 / tauS
  const e = Math.exp(-w * dtS)
  let e0 = g.az - g.azT
  let c = g.vaz + w * e0
  g.az = g.azT + (e0 + c * dtS) * e
  g.vaz = (g.vaz - w * c * dtS) * e
  e0 = g.el - g.elT
  c = g.vel + w * e0
  g.el = g.elT + (e0 + c * dtS) * e
  g.vel = (g.vel - w * c * dtS) * e
}
