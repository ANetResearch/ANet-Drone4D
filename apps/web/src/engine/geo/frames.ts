import { hypot4 } from '../hypot'
// The single TypeScript frame-conversion implementation (M02 §7.2; AWR-03 §5.1 rules 5-8).
// Isomorphic subset of python/awr/world/georef/frames.py: geodesy (WGS84/CGCS2000, LLA, ECEF, world ENU),
// ENU<->NED, FLU<->FRD, ENU<->three (E, U, -N), heading, UE flight files, Sim3 interpolation.
// PX4 local projection and time scales stay Python-only. Every function writes into `out`; nothing allocates
// per call (M02-FR-009, M02-NFR-005). No other module may re-implement these formulas.

export interface Ellipsoid {
  readonly a: number
  readonly f: number
  readonly e2: number
  readonly b: number
}

const ell = (a: number, invF: number): Ellipsoid => {
  const f = 1 / invF
  return { a, f, e2: f * (2 - f), b: a * (1 - f) }
}
export const WGS84: Ellipsoid = ell(6378137.0, 298.257223563)
export const CGCS2000: Ellipsoid = ell(6378137.0, 298.257222101)

const DEG = Math.PI / 180
const RAD2DEG = 180 / Math.PI
const INV_SQRT2 = 1 / Math.SQRT2

/** three = R_THREE_ENU * enu, row-major: (x, y, z)_three = (E, U, -N); WorldLayer rotation.x = -pi/2 (ADR-002). */
export const R_THREE_ENU: readonly number[] = [1, 0, 0, 0, 0, 1, 0, -1, 0]

/** Precomputed anchor (coordinate.json anchor): R is R_enu_ecef row-major, p0 the anchor in ECEF. */
export interface AnchorF64 {
  readonly lat: number
  readonly lon: number
  readonly h: number
  readonly ellipsoid: Ellipsoid
  readonly p0: Float64Array
  readonly R: Float64Array
}

export interface AnchorJson {
  latDeg: number
  lonDeg: number
  hEllipsoidM: number
  datum?: string
}

// ---------------------------------------------------------------- geodesy
export function llaToEcefInto(out: Float64Array | number[], latDeg: number, lonDeg: number, h: number, e: Ellipsoid = WGS84): Float64Array | number[] {
  const lat = latDeg * DEG, lon = lonDeg * DEG
  const sl = Math.sin(lat), cl = Math.cos(lat)
  const n = e.a / Math.sqrt(1 - e.e2 * sl * sl)
  out[0] = (n + h) * cl * Math.cos(lon)
  out[1] = (n + h) * cl * Math.sin(lon)
  out[2] = (n * (1 - e.e2) + h) * sl
  return out
}

/** Zhu (1994) closed form, identical to frames.py; out = [lat_deg, lon_deg, h_m]. */
export function ecefToLlaInto(out: Float64Array | number[], x: number, y: number, z: number, e: Ellipsoid = WGS84): Float64Array | number[] {
  const a = e.a, b = e.b, e2 = e.e2
  const ep2 = (a * a - b * b) / (b * b)
  const r2 = x * x + y * y
  const r = Math.sqrt(r2)
  if (r < 1e-9) {
    out[0] = z >= 0 ? 90 : -90
    out[1] = 0
    out[2] = Math.abs(z) - b
    return out
  }
  const F = 54 * b * b * z * z
  const G = r2 + (1 - e2) * z * z - e2 * (a * a - b * b)
  const c = (e2 * e2 * F * r2) / (G * G * G)
  const s = Math.cbrt(1 + c + Math.sqrt(c * c + 2 * c))
  const P = F / (3 * (s + 1 / s + 1) ** 2 * G * G)
  const Q = Math.sqrt(1 + 2 * e2 * e2 * P)
  const r0 = -(P * e2 * r) / (1 + Q) + Math.sqrt(Math.max(0.5 * a * a * (1 + 1 / Q) - (P * (1 - e2) * z * z) / (Q * (1 + Q)) - 0.5 * P * r2, 0))
  const U = Math.sqrt((r - e2 * r0) ** 2 + z * z)
  const V = Math.sqrt((r - e2 * r0) ** 2 + (1 - e2) * z * z)
  const z0 = (b * b * z) / (a * V)
  out[0] = Math.atan((z + ep2 * z0) / r) * RAD2DEG
  out[1] = Math.atan2(y, x) * RAD2DEG
  out[2] = U * (1 - (b * b) / (a * V))
  return out
}

/** Build once when a world opens; returns null for an invalid anchor (UI then shows the unknown value, AWR-03 §5.7). */
export function makeAnchor(a: AnchorJson | null | undefined): AnchorF64 | null {
  if (!a || !Number.isFinite(a.latDeg) || !Number.isFinite(a.lonDeg) || !Number.isFinite(a.hEllipsoidM)) return null
  if (a.datum !== undefined && a.datum !== 'WGS84' && a.datum !== 'CGCS2000') return null
  const e = a.datum === 'CGCS2000' ? CGCS2000 : WGS84
  const la = a.latDeg * DEG, lo = a.lonDeg * DEG
  const R = new Float64Array([
    -Math.sin(lo), Math.cos(lo), 0,
    -Math.sin(la) * Math.cos(lo), -Math.sin(la) * Math.sin(lo), Math.cos(la),
    Math.cos(la) * Math.cos(lo), Math.cos(la) * Math.sin(lo), Math.sin(la),
  ])
  const p0 = new Float64Array(3)
  llaToEcefInto(p0, a.latDeg, a.lonDeg, a.hEllipsoidM, e)
  return { lat: a.latDeg, lon: a.lonDeg, h: a.hEllipsoidM, ellipsoid: e, p0, R }
}

const tmp3 = new Float64Array(3)

/** world ENU (m) -> [lat_deg, lon_deg, h_ellipsoid_m], strictly through ECEF. */
export function worldToLlaInto(out: Float64Array | number[], e: number, n: number, u: number, a: AnchorF64): Float64Array | number[] {
  const R = a.R, p0 = a.p0
  const x = R[0] * e + R[3] * n + R[6] * u + p0[0]
  const y = R[1] * e + R[4] * n + R[7] * u + p0[1]
  const z = R[2] * e + R[5] * n + R[8] * u + p0[2]
  return ecefToLlaInto(out, x, y, z, a.ellipsoid)
}

export function llaToWorldInto(out: Float64Array | number[], latDeg: number, lonDeg: number, h: number, a: AnchorF64): Float64Array | number[] {
  llaToEcefInto(tmp3, latDeg, lonDeg, h, a.ellipsoid)
  const dx = tmp3[0] - a.p0[0], dy = tmp3[1] - a.p0[1], dz = tmp3[2] - a.p0[2]
  const R = a.R
  out[0] = R[0] * dx + R[1] * dy + R[2] * dz
  out[1] = R[3] * dx + R[4] * dy + R[5] * dz
  out[2] = R[6] * dx + R[7] * dy + R[8] * dz
  return out
}

// ---------------------------------------------------------------- ENU/NED, FLU/FRD
/** (E, N, U) -> (N, E, -U); an involution (also NED -> ENU). */
export function enuToNedInto(out: Float64Array | number[], e: number, n: number, u: number): Float64Array | number[] {
  out[0] = n
  out[1] = e
  out[2] = -u
  return out
}

/** B = diag(1, -1, -1); an involution (also FRD -> FLU). */
export function fluToFrdInto(out: Float64Array | number[], x: number, y: number, z: number): Float64Array | number[] {
  out[0] = x
  out[1] = -y
  out[2] = -z
  return out
}

/** q' = (1/sqrt2)(w+z, x+y, x-y, w-z) for NED/FRD wxyz input; out is ENU/FLU xyzw with w >= 0. */
export function quatEnuFluFromNedFrdInto(out: Float64Array | number[], w: number, x: number, y: number, z: number): Float64Array | number[] {
  const nw = (w + z) * INV_SQRT2, nx = (x + y) * INV_SQRT2, ny = (x - y) * INV_SQRT2, nz = (w - z) * INV_SQRT2
  const s = nw < 0 ? -1 : 1
  out[0] = nx * s
  out[1] = ny * s
  out[2] = nz * s
  out[3] = nw * s
  return out
}

/** Floor modulo with numpy semantics (np.mod), so wrap and heading agree bit for bit with frames.py. */
const pymod = (x: number, y: number): number => {
  let r = x % y
  if (r !== 0) {
    if (r < 0 !== y < 0) r += y
  } else r = y < 0 ? -0 : 0
  return r
}

const wrapPi = (a: number): number => {
  const w = pymod(a + Math.PI, 2 * Math.PI) - Math.PI
  return w === -Math.PI ? Math.PI : w
}

/** yaw_ned = pi/2 - psi_enu wrapped to (-pi, pi] (also the inverse). */
export const yawNedFromEnu = (psiEnu: number): number => wrapPi(Math.PI / 2 - psiEnu)

/** HUD heading: degrees, north = 0, clockwise: ((90 - psi_deg) mod 360 + 360) mod 360 (AWR-03 §5.3). */
export function headingDeg(psiEnuRad: number): number {
  return pymod(90 - psiEnuRad * RAD2DEG, 360)
}
export const yawEnuFromHeadingDeg = (h: number): number => wrapPi((90 - h) * DEG)

// ---------------------------------------------------------------- three
/** (E, N, U) -> (E, U, -N). */
export function enuToThreeInto(out: Float64Array | number[], e: number, n: number, u: number): Float64Array | number[] {
  out[0] = e
  out[1] = u
  out[2] = -n
  return out
}

/** (x, y, z) -> (x, -z, y). */
export function threeToEnuInto(out: Float64Array | number[], x: number, y: number, z: number): Float64Array | number[] {
  out[0] = x
  out[1] = -z
  out[2] = y
  return out
}

/** Orientation (xyzw) in ENU -> three: q_R * q * q_R^-1 with q_R = rotation about x by -pi/2. */
export function quatEnuToThreeInto(out: Float64Array | number[], x: number, y: number, z: number, w: number): Float64Array | number[] {
  // q_R = (-s, 0, 0, s), s = 1/sqrt2; conjugation by a rotation about x maps (x, y, z) -> (x, z, -y)
  out[0] = x
  out[1] = z
  out[2] = -y
  out[3] = w
  return out
}

// ---------------------------------------------------------------- UE flight files (x01 §3.5(a))
/** E = Y/100 + tE, N = X/100 + tN, U = Z/100 + tU. */
export function ueCmToWorldInto(out: Float64Array | number[], x: number, y: number, z: number, tE: number, tN: number, tU: number): Float64Array | number[] {
  out[0] = y / 100 + tE
  out[1] = x / 100 + tN
  out[2] = z / 100 + tU
  return out
}

/** psi = 90 deg - yaw, theta = pitch (nose down positive), q = Rz(psi) Ry(theta) Rx(phi), xyzw with w >= 0. */
export function ueRotToQuatInto(out: Float64Array | number[], pitchDeg: number, rollDeg: number, yawDeg: number): Float64Array | number[] {
  const hz = ((90 - yawDeg) * DEG) / 2, hy = (pitchDeg * DEG) / 2, hx = (rollDeg * DEG) / 2
  const cz = Math.cos(hz), sz = Math.sin(hz), cy = Math.cos(hy), sy = Math.sin(hy), cx = Math.cos(hx), sx = Math.sin(hx)
  // A = qz * qy = (-sz sy, cz sy, sz cy, cz cy); q = A * qx
  const ax = -sz * sy, ay = cz * sy, az = sz * cy, aw = cz * cy
  let qx = aw * sx + ax * cx
  let qy = ay * cx + az * sx
  let qz = az * cx - ay * sx
  let qw = aw * cx - ax * sx
  const n = hypot4(qx, qy, qz, qw)
  const s = qw < 0 ? -1 / n : 1 / n
  qx *= s
  qy *= s
  qz *= s
  qw *= s
  out[0] = qx
  out[1] = qy
  out[2] = qz
  out[3] = qw
  return out
}

// ---------------------------------------------------------------- Sim3 (x_to = s R(q) x_from + t)
export interface Sim3 {
  readonly s: number
  readonly q: readonly [number, number, number, number]
  readonly t: readonly [number, number, number]
}

/** out = [s, qx, qy, qz, qw, tx, ty, tz]: s log-linear, q slerp (shortest path), t linear; exact at u = 0, 1. */
export function sim3InterpolateInto(out: Float64Array | number[], a: Sim3, b: Sim3, u: number): Float64Array | number[] {
  if (u <= 0 || u >= 1) {
    const c = u <= 0 ? a : b
    out[0] = c.s
    out[1] = c.q[0]; out[2] = c.q[1]; out[3] = c.q[2]; out[4] = c.q[3]
    out[5] = c.t[0]; out[6] = c.t[1]; out[7] = c.t[2]
    return out
  }
  out[0] = Math.exp((1 - u) * Math.log(a.s) + u * Math.log(b.s))
  let bx = b.q[0], by = b.q[1], bz = b.q[2], bw = b.q[3]
  let d = a.q[0] * bx + a.q[1] * by + a.q[2] * bz + a.q[3] * bw
  if (d < 0) {
    bx = -bx; by = -by; bz = -bz; bw = -bw; d = -d
  }
  let ka: number, kb: number
  if (d > 0.9995) {
    ka = 1 - u
    kb = u
  } else {
    const th = Math.acos(Math.min(1, d))
    ka = Math.sin((1 - u) * th) / Math.sin(th)
    kb = Math.sin(u * th) / Math.sin(th)
  }
  let qx = ka * a.q[0] + kb * bx, qy = ka * a.q[1] + kb * by, qz = ka * a.q[2] + kb * bz, qw = ka * a.q[3] + kb * bw
  const n = hypot4(qx, qy, qz, qw)
  const sg = qw < 0 ? -1 / n : 1 / n
  qx *= sg; qy *= sg; qz *= sg; qw *= sg
  out[1] = qx; out[2] = qy; out[3] = qz; out[4] = qw
  out[5] = (1 - u) * a.t[0] + u * b.t[0]
  out[6] = (1 - u) * a.t[1] + u * b.t[1]
  out[7] = (1 - u) * a.t[2] + u * b.t[2]
  return out
}

/** M02 §7.2 name; same function as sim3InterpolateInto. */
export const sim3Interpolate = sim3InterpolateInto
