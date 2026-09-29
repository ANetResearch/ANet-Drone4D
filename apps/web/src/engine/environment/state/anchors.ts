// Integral anchors on the deterministic 20 ms grid (M07-FR-018; M07 §6.2.3, §6.3.10; g06 §3.4). Owner: M07.
// Line-for-line mirror of python/awr/environment/anchors.py: trapezoid on t_k = k H (H = 20 ms) for S, D, fall_rain,
// fall_snow; wetness and puddle as first-order exponentials with the rate at the step start. Both ends run the same
// operation sequence, so after 1 h the difference stays <= 1e-6 m (M07-NFR-013). Zero allocation (module scratch).
import { eDir } from './conventions'
import { derive, newDerived, puddleTarget } from './derive'
import { evalEnv, type Transition } from './evalEnv'
import type { Anchors } from './keyframe'
import { F, NF, type ProfileCfg } from './presets'

export const H_NS = 20_000_000
const H_S = H_NS * 1e-9

const sBuf = new Float64Array(NF)
const dBuf = newDerived()
const eBuf = new Float64Array(2)

/** rates at t: [speed, speed ex, speed ey, w_mean, v_rain, v_snow, wet_target, puddle_target] */
export function rates(kf: Transition, tNs: number, out: Float64Array, prof: ProfileCfg | null = null): Float64Array {
  const s = evalEnv(kf, tNs, sBuf)
  const d = derive(s, dBuf, prof, kf.P)
  eDir(s[F.DIR], eBuf)
  out[0] = s[F.SPEED_REF]
  out[1] = s[F.SPEED_REF] * eBuf[0]
  out[2] = s[F.SPEED_REF] * eBuf[1]
  out[3] = s[F.W_MEAN]
  out[4] = d.v_rain_mps
  out[5] = d.v_snow_mps
  out[6] = d.wet_target
  out[7] = puddleTarget(d, kf.P)
  return out
}

export function anchorsInitial(kf: Transition, tNs: number, A: Anchors): Anchors {
  const d = derive(evalEnv(kf, tNs, sBuf), dBuf, null, kf.P)
  A.tNs = tNs
  A.sM = 0
  A.d[0] = 0
  A.d[1] = 0
  A.d[2] = 0
  A.fallRain = 0
  A.fallSnow = 0
  A.wetness = d.wet_target
  A.puddle = puddleTarget(d, kf.P)
  return A
}

/** one grid step with rates r0 (step start) and r1 (step end) */
export function step(A: Anchors, r0: Float64Array, r1: Float64Array, tau: Float64Array): void {
  const h = H_S
  A.sM += 0.5 * h * (r0[0] + r1[0])
  A.d[0] = A.d[0] + 0.5 * h * (r0[1] + r1[1])
  A.d[1] = A.d[1] + 0.5 * h * (r0[2] + r1[2])
  A.d[2] = A.d[2] + 0.5 * h * (r0[3] + r1[3])
  A.fallRain += 0.5 * h * (r0[4] + r1[4])
  A.fallSnow += 0.5 * h * (r0[5] + r1[5])
  const tw = r0[6] > A.wetness ? tau[0] : tau[1]
  A.wetness += (r0[6] - A.wetness) * (1.0 - Math.exp(-h / tw))
  const tp = r0[7] > A.puddle ? tau[2] : tau[3]
  A.puddle += (r0[7] - A.puddle) * (1.0 - Math.exp(-h / tp))
}

const r0Buf = new Float64Array(8)
const r1Buf = new Float64Array(8)
const tauBuf = new Float64Array(4)

function taus(kf: Transition): Float64Array {
  const c = kf.P.c
  tauBuf[0] = c.wetness_tau_up_s
  tauBuf[1] = c.wetness_tau_down_s
  tauBuf[2] = c.puddle_tau_up_s
  tauBuf[3] = c.puddle_tau_down_s
  return tauBuf
}

/** reference advance from grid k0 to k1 (same as the Python `advance`) */
export function advance(A: Anchors, kf: Transition, k0: number, k1: number): Anchors {
  const tau = taus(kf)
  let r0: Float64Array = rates(kf, k0 * H_NS, r0Buf)
  let r1: Float64Array = r1Buf
  for (let k = k0; k < k1; k++) {
    rates(kf, (k + 1) * H_NS, r1)
    step(A, r0, r1, tau)
    const t = r0
    r0 = r1
    r1 = t
  }
  if (r0 !== r0Buf) r0Buf.set(r0)
  A.tNs = k1 * H_NS
  return A
}

/** value inside a grid cell: A at t_k = floor(t/H) H, trapezoid partial value, exact exponential for wetness */
export function partial(A: Anchors, kf: Transition, tNs: number, out: Anchors): Anchors {
  out.tNs = A.tNs
  out.sM = A.sM
  out.d[0] = A.d[0]
  out.d[1] = A.d[1]
  out.d[2] = A.d[2]
  out.fallRain = A.fallRain
  out.fallSnow = A.fallSnow
  out.wetness = A.wetness
  out.puddle = A.puddle
  const k = Math.floor(tNs / H_NS)
  const tk = k * H_NS
  if (tNs <= tk) return out
  const tau = taus(kf)
  const r0 = rates(kf, tk, r0Buf)
  const r1 = rates(kf, tNs, r1Buf)
  const dt = (tNs - tk) * 1e-9
  out.sM += 0.5 * dt * (r0[0] + r1[0])
  out.d[0] += 0.5 * dt * (r0[1] + r1[1])
  out.d[1] += 0.5 * dt * (r0[2] + r1[2])
  out.d[2] += 0.5 * dt * (r0[3] + r1[3])
  out.fallRain += 0.5 * dt * (r0[4] + r1[4])
  out.fallSnow += 0.5 * dt * (r0[5] + r1[5])
  const tw = r0[6] > out.wetness ? tau[0] : tau[1]
  out.wetness += (r0[6] - out.wetness) * (1.0 - Math.exp(-dt / tw))
  const tp = r0[7] > out.puddle ? tau[2] : tau[3]
  out.puddle += (r0[7] - out.puddle) * (1.0 - Math.exp(-dt / tp))
  out.tNs = tNs
  return out
}

/** backward trapezoid A_k = A_{k+1} - h (r_k + r_{k+1}) / 2 for S, D, fall (first frame later than tRender, §6.3.10) */
export function backstep(A: Anchors, kf: Transition, k1: number, k0: number): Anchors {
  const h = H_S
  let r1: Float64Array = rates(kf, k1 * H_NS, r1Buf)
  let r0: Float64Array = r0Buf
  for (let k = k1; k > k0; k--) {
    rates(kf, (k - 1) * H_NS, r0)
    A.sM -= 0.5 * h * (r0[0] + r1[0])
    A.d[0] -= 0.5 * h * (r0[1] + r1[1])
    A.d[1] -= 0.5 * h * (r0[2] + r1[2])
    A.d[2] -= 0.5 * h * (r0[3] + r1[3])
    A.fallRain -= 0.5 * h * (r0[4] + r1[4])
    A.fallSnow -= 0.5 * h * (r0[5] + r1[5])
    const t = r1
    r1 = r0
    r0 = t
  }
  A.tNs = k0 * H_NS
  return A
}
