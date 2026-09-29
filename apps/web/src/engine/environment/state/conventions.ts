// Direction and frame conversions, the single TS implementation (M07-FR-003; M07 §6.3.1; g06 §2.2; AWR-03 §5.1 rule 8).
// Owner: M07. Line-for-line mirror of python/awr/environment/conventions.py, checked against golden conventions.json.
// Wind direction is the meteorological "from" direction (clockwise from grid north, degrees); wind vectors are "to"
// vectors (ENU m/s). three frame = (E, U, -N); NED = (N, E, -U). Python's % is a non-negative modulo: mod() below.

const DEG = Math.PI / 180 // math.radians: x * (pi / 180)
const RAD = 180 / Math.PI // math.degrees: x * (180 / pi)
export const CALM_MPS = 1e-6

/** CPython float modulo (float_rem): fmod, then + b when the signs differ; a zero result takes the sign of b */
export function mod(a: number, b: number): number {
  let r = a % b
  if (r !== 0) {
    if (b < 0 !== r < 0) r += b
  } else r = b < 0 ? -0 : 0
  return r
}

export function fromToUv(speed: number, dirFromDeg: number, out: Float64Array): Float64Array {
  const t = dirFromDeg * DEG
  out[0] = -speed * Math.sin(t)
  out[1] = -speed * Math.cos(t)
  return out
}

/** out = [speed, dir_from_deg, calm (0/1)] */
export function uvToFrom(u: number, v: number, out: Float64Array, calm = CALM_MPS): Float64Array {
  const s = Math.hypot(u, v)
  if (s < calm) {
    out[0] = 0
    out[1] = 0
    out[2] = 1
    return out
  }
  out[0] = s
  out[1] = mod(Math.atan2(-u, -v) * RAD + 360.0, 360.0)
  out[2] = 0
  return out
}

/** going-to unit vector of a from-direction */
export function eDir(dirFromDeg: number, out: Float64Array): Float64Array {
  const t = dirFromDeg * DEG
  out[0] = -Math.sin(t)
  out[1] = -Math.cos(t)
  return out
}

/** e rotated 90 degrees counter-clockwise */
export function nDir(dirFromDeg: number, out: Float64Array): Float64Array {
  eDir(dirFromDeg, out)
  const ex = out[0]
  out[0] = -out[1]
  out[1] = ex
  return out
}

export function shortestArc(a: number, b: number): number {
  return mod(b - a + 540.0, 360.0) - 180.0
}

export function enuToThree(u: number, v: number, w: number, out: Float64Array): Float64Array {
  out[0] = u
  out[1] = w
  out[2] = -v
  return out
}

export function threeToEnu(x: number, y: number, z: number, out: Float64Array): Float64Array {
  out[0] = x
  out[1] = -z
  out[2] = y
  return out
}

export function enuToNed(u: number, v: number, w: number, out: Float64Array): Float64Array {
  out[0] = v
  out[1] = u
  out[2] = -w
  return out
}
