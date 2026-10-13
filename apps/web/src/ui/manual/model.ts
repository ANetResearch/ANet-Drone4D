// Manual control model (R-D2-21; AWR-04 §10.6; M15-FR-215 to FR-217; UX-FR-133 to UX-FR-135; AWR-14 §20.10.2 to §20.10.4,
// §20.18). Pure functions, no allocation in the 30 Hz path: the input shaping (dead zone 0.08, expo 0.3), the keyboard
// first-order smoothing (150 ms), the multirotor body-frame mapping (FLU: x forward, y left, z up, yaw rate
// counter-clockwise positive) with the speed tiers of AWR-14 §20.18 (horizontal 2 / 5 / min(vmax, 10) m/s, vertical 1 / 2
// / 3 m/s, yaw 0.4 / 0.8 / 1.2 rad/s), the fixed-wing mapping (frame "fw": vel[0] commanded airspeed integrated at 2 m/s²
// and clamped to [V_min, V_max], vel[1] unused, vel[2] climb rate, yaw rate = turn rate up to g tan(phi_max) / V_a;
// AWR-17 §17.7.6) and the 3 s keep-out approach prediction (signed distance to each enabled volume, spheres and
// hemispheres in 3D, cylinders and sectors horizontally, moving targets' sectors as full circles). The soft fence itself
// is server-side (sim-core clamps toward keep-out volumes, `keepout_guard: clamp`); the UI only warns.

export type SpeedTier = 'slow' | 'normal' | 'fast'
export const TIERS: readonly SpeedTier[] = ['slow', 'normal', 'fast']
export const DEAD_ZONE = 0.08
export const EXPO = 0.3
export const KEY_TAU_S = 0.15
export const PUMP_HZ = 30
export const ZERO_TAIL = 5
export const PREDICT_S = 3
export const G = 9.80665

export interface McLimits { vmaxMps: number }
export interface FwLimits { vMinMps: number; vMaxMps: number; vCruiseMps: number; bankMaxRad: number; climbMaxMps: number }

const MC_H: Readonly<Record<SpeedTier, number>> = { slow: 2, normal: 5, fast: 10 }
const MC_V: Readonly<Record<SpeedTier, number>> = { slow: 1, normal: 2, fast: 3 }
const MC_YAW: Readonly<Record<SpeedTier, number>> = { slow: 0.4, normal: 0.8, fast: 1.2 }
const FW_FRAC: Readonly<Record<SpeedTier, number>> = { slow: 0.33, normal: 0.66, fast: 1 }

export function nextTier(t: SpeedTier): SpeedTier {
  return TIERS[(TIERS.indexOf(t) + 1) % TIERS.length]
}

/** multirotor tier speeds (the fast horizontal tier is capped by the model's vmax) */
export function mcTier(t: SpeedTier, lim: McLimits = { vmaxMps: 10 }): { h: number; v: number; yaw: number } {
  return { h: t === 'fast' ? Math.min(lim.vmaxMps, MC_H.fast) : MC_H[t], v: MC_V[t], yaw: MC_YAW[t] }
}

/** dead zone then expo: u' = sign(u) (0.7 |u| + 0.3 |u|^3) on the renormalised magnitude */
export function shape(u: number): number {
  const a = Math.abs(u)
  if (!Number.isFinite(u) || a <= DEAD_ZONE) return 0
  const x = Math.min(1, (a - DEAD_ZONE) / (1 - DEAD_ZONE))
  return Math.sign(u) * ((1 - EXPO) * x + EXPO * x * x * x)
}

/** one first-order step of a key axis toward its target (-1, 0, 1) */
export function smooth(cur: number, target: number, dtS: number, tau = KEY_TAU_S): number {
  const k = 1 - Math.exp(-Math.max(0, dtS) / tau)
  const v = cur + (target - cur) * k
  return Math.abs(v - target) < 1e-3 ? target : v
}

/** normalised inputs in [-1, 1]: forward, right, up, yawRight (keys and sticks merged, larger magnitude wins) */
export interface ManualInput { fwd: number; right: number; up: number; yawRight: number }
export const ZERO_INPUT: Readonly<ManualInput> = { fwd: 0, right: 0, up: 0, yawRight: 0 }
export const isZero = (i: ManualInput): boolean => i.fwd === 0 && i.right === 0 && i.up === 0 && i.yawRight === 0

/** merge keyboard and stick axes: the larger magnitude wins per axis */
export function merge(keys: ManualInput, stick: ManualInput, out: ManualInput): ManualInput {
  out.fwd = Math.abs(stick.fwd) > Math.abs(keys.fwd) ? stick.fwd : keys.fwd
  out.right = Math.abs(stick.right) > Math.abs(keys.right) ? stick.right : keys.right
  out.up = Math.abs(stick.up) > Math.abs(keys.up) ? stick.up : keys.up
  out.yawRight = Math.abs(stick.yawRight) > Math.abs(keys.yawRight) ? stick.yawRight : keys.yawRight
  return out
}

/** multirotor setpoint into out[vx, vy, vz, yawRate] (body FLU, m/s and rad/s) */
export function mcSetpoint(inp: ManualInput, tier: SpeedTier, lim: McLimits, out: Float32Array): Float32Array {
  const k = mcTier(tier, lim)
  out[0] = shape(inp.fwd) * k.h
  out[1] = -shape(inp.right) * k.h
  out[2] = shape(inp.up) * k.v
  out[3] = -shape(inp.yawRight) * k.yaw
  return out
}

/** fixed-wing state carried between pump ticks (the commanded airspeed) */
export interface FwState { vCmd: number }

/** fixed-wing setpoint into out[airspeed, NaN, climb, turnRate] (frame fw) */
export function fwSetpoint(inp: ManualInput, tier: SpeedTier, lim: FwLimits, st: FwState, dtS: number, airspeedMps: number, out: Float32Array): Float32Array {
  const frac = FW_FRAC[tier]
  if (!Number.isFinite(st.vCmd) || st.vCmd <= 0) st.vCmd = lim.vCruiseMps
  st.vCmd = Math.min(lim.vMaxMps, Math.max(lim.vMinMps, st.vCmd + shape(inp.fwd) * 2 * dtS))
  const va = Math.max(lim.vMinMps, Number.isFinite(airspeedMps) && airspeedMps > 0 ? airspeedMps : st.vCmd)
  const turnMax = (G * Math.tan(lim.bankMaxRad)) / va
  // A/D and the yaw keys both turn; the larger request wins
  const turnIn = Math.abs(inp.yawRight) > Math.abs(inp.right) ? inp.yawRight : inp.right
  out[0] = st.vCmd
  out[1] = Number.NaN
  out[2] = shape(inp.up) * frac * lim.climbMaxMps
  out[3] = -shape(turnIn) * frac * turnMax
  return out
}

/** body FLU velocity to world ENU with the heading (yaw, rad, ENU, counter-clockwise from east) */
export function bodyToWorld(vx: number, vy: number, yaw: number): [number, number] {
  const c = Math.cos(yaw)
  const s = Math.sin(yaw)
  return [c * vx - s * vy, s * vx + c * vy]
}

// ------------------------------------------------------------ keep-out approach prediction (UI warning only)
export type VolShape = 'sphere' | 'hemisphere' | 'cylinder' | 'sector'
export interface KeepVolume {
  target: string
  kind: 'visual' | 'acoustic' | 'radar' | 'rf'
  shape: VolShape
  /** centre ENU m (the target position) */
  c: readonly [number, number, number]
  /** keep-out radius m (configured or effective radius plus the margin) */
  r: number
  zMin: number
  zMax: number
}

/** signed distance from p to the volume (outside positive) and the outward normal direction (unit) */
export function signedDistance(p: readonly number[], v: KeepVolume): { d: number; n: [number, number, number] } {
  const dx = p[0] - v.c[0]
  const dy = p[1] - v.c[1]
  const dz = p[2] - v.c[2]
  if (v.shape === 'sphere' || v.shape === 'hemisphere') {
    const below = v.shape === 'hemisphere' && dz < 0
    const zz = below ? 0 : dz
    const len = Math.hypot(dx, dy, zz)
    if (len < 1e-9) return { d: -v.r, n: [0, 0, 1] }
    return { d: len - v.r, n: [dx / len, dy / len, zz / len] }
  }
  // cylinders and sectors: horizontal distance inside the height band (a moving target's sector is a full circle)
  const h = Math.hypot(dx, dy)
  const zAbs = p[2]
  const outsideBand = zAbs > v.c[2] + v.zMax ? zAbs - (v.c[2] + v.zMax) : zAbs < v.c[2] + v.zMin ? (v.c[2] + v.zMin) - zAbs : 0
  if (h < 1e-9) return { d: outsideBand > 0 ? outsideBand : -v.r, n: [0, 0, outsideBand > 0 && zAbs > v.c[2] ? 1 : -1] }
  return { d: Math.max(h - v.r, outsideBand), n: [dx / h, dy / h, 0] }
}

export interface Approach { target: string; kind: KeepVolume['kind']; tS: number; d: number }

/**
 * the soonest entry within `horizonS` at the current world velocity (vel ENU m/s): t = d / max(eps, -v . n); inside a
 * volume t = 0; null when nothing is entered within the horizon
 */
export function predictEntry(p: readonly number[], vel: readonly number[], vols: readonly KeepVolume[], horizonS = PREDICT_S): Approach | null {
  let best: Approach | null = null
  for (const v of vols) {
    const { d, n } = signedDistance(p, v)
    let tS: number
    if (d <= 0) tS = 0
    else {
      const closing = -(vel[0] * n[0] + vel[1] * n[1] + vel[2] * n[2])
      if (closing <= 1e-6) continue
      tS = d / closing
    }
    if (tS <= horizonS && (!best || tS < best.tS)) best = { target: v.target, kind: v.kind, tS, d }
  }
  return best
}
