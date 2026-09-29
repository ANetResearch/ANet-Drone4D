// eval_env (M07-FR-004; M07 §6.3.2; g06 §3.3). Owner: M07. Line-for-line mirror of
// python/awr/environment/weather/transitions.py (golden eval_env.json): step (t >= t0 gives `to`), smooth (the route
// split evenly by segments, per-group window smoothstep, enter/leave chosen by precip_level = rain + 10 snow), exp (per
// group first-order rate, t1 = t0 + ceil(12 / min(rates)) s snaps to `to`). Spaces: lin, log (MOR), arc (shortest arc,
// result modulo 360). Zero allocation: writes into `out`; the route vectors are built at decode time.
import { mod, shortestArc } from './conventions'
import { smoothstep } from './derive'
import { MODE_EXP, MODE_SMOOTH, MODE_STEP, precipLevel, type EnvKeyframe } from './keyframe'
import { NF, PRESETS_MODEL, type PresetsModel } from './presets'

/** the part of a keyframe eval_env reads */
export type Transition = Pick<EnvKeyframe, 'P' | 'mode' | 't0Ns' | 't1Ns' | 'from' | 'to' | 'route' | 'routeEnter'>

export function interp(space: number, a: number, b: number, k: number): number {
  if (space === 1) return Math.exp(Math.log(a) + (Math.log(b) - Math.log(a)) * k)
  if (space === 2) return mod(a + shortestArc(a, b) * k, 360.0)
  return a + (b - a) * k
}

export function evalEnv(kf: Transition, tNs: number, out: Float64Array): Float64Array {
  const P = kf.P
  if (kf.mode === MODE_STEP || tNs >= kf.t1Ns) {
    for (let i = 0; i < NF; i++) out[i] = kf.to[i]
    return out
  }
  if (tNs <= kf.t0Ns) {
    for (let i = 0; i < NF; i++) out[i] = kf.from[i]
    return out
  }
  if (kf.mode === MODE_EXP) {
    const dt = (tNs - kf.t0Ns) * 1e-9
    for (let i = 0; i < NF; i++) out[i] = interp(P.space[i], kf.from[i], kf.to[i], 1.0 - Math.exp(-P.rate[i] * dt))
    return out
  }
  const route = kf.route
  const nseg = route.length - 1
  const x = (tNs - kf.t0Ns) / (kf.t1Ns - kf.t0Ns)
  const seg = Math.min(Math.trunc(x * nseg), nseg - 1)
  const xl = x * nseg - seg
  const A = route[seg]
  const B = route[seg + 1]
  const W = kf.routeEnter[seg] === 1 ? P.winEnter : P.winLeave
  for (let i = 0; i < NF; i++) out[i] = interp(P.space[i], A[i], B[i], smoothstep(W[2 * i], W[2 * i + 1], xl))
  return out
}

/** a transition from plain values (golden cases, tests); allocates */
export function makeTransition(mode: 'step' | 'smooth' | 'exp', t0Ns: number, t1Ns: number, from: ArrayLike<number>, to: ArrayLike<number>,
  via: readonly string[] = [], P: PresetsModel = PRESETS_MODEL): Transition {
  const f = Float64Array.from(from as ArrayLike<number>).subarray(0, NF)
  const t = Float64Array.from(to as ArrayLike<number>).subarray(0, NF)
  const route = [f, ...via.map((id) => P.overlay(t, id, new Float64Array(NF))), t]
  const routeEnter = new Uint8Array(route.length - 1)
  for (let i = 0; i < route.length - 1; i++) routeEnter[i] = precipLevel(route[i + 1]) > precipLevel(route[i]) ? 1 : 0
  return { P, mode: mode === 'step' ? MODE_STEP : mode === 'exp' ? MODE_EXP : MODE_SMOOTH, t0Ns, t1Ns, from: f, to: t, route, routeEnter }
}

/** exp mode: t1 = t0 + ceil(exp_settle / min(rates)) s */
export function expT1Ns(t0Ns: number, P: PresetsModel = PRESETS_MODEL): number {
  let mn = Number.POSITIVE_INFINITY
  for (let i = 0; i < P.nf; i++) mn = Math.min(mn, P.rate[i])
  return t0Ns + Math.ceil(P.c.exp_settle / mn) * 1_000_000_000
}
