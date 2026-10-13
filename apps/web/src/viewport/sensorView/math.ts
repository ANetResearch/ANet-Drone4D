// Sensor view parameters (D2 WP-11; AWR-04 §6.1, §6.2, §10.5, ADR-108; AWR-14 §20.10.5). Owner: M06.
// Pure CPU side of the three visual approximations (no three objects, unit tested in Node): the passive light factor of
// the EO and night views (M19's k_light curves, AWR-04 §6.2: lg(E / E_min) / 2 with E_min 0.5 lx colour and 0.05 lx mono),
// the active 850 nm illumination profile (cone, 1/R^2 fall-off, the k_light edge clamp((R_nir - R) / (0.2 R_nir)) of
// M19), the LWIR apparent temperature of an emitter (emissivity mix with the sky, path transmittance towards the air
// temperature) and the automatic gain range of the thermal view, and the grey palette ramp (scene tokens pcRamp, the
// neutral Graphite ramp: no hue, so the thermal view has no red pixel, AWR-04 §10.7 and D2-AC-25). These views are a
// visual approximation only (ADR-108): every criterion value shown on the HUD comes from uav/{id}/perception.
import { SCENE } from '@/lib/tokens/scene.gen'

export type SensorViewMode = 'fpv' | 'eo' | 'nir' | 'lwir'
export type LwirPalette = 'white_hot' | 'black_hot'
export const SENSOR_VIEW_MODES: readonly SensorViewMode[] = ['fpv', 'eo', 'nir', 'lwir']

export const SV = {
  /** E_min of the colour and mono passive curves (lx, AWR-04 §6.1, §6.2) */
  eMinColorLx: 0.5,
  eMinMonoLx: 0.05,
  /** active NIR: soft cone edge (fraction of the half angle), edge band of k_light (fraction of R_nir) */
  coneSoft: 0.15,
  nirEdgeFrac: 0.2,
  /** distance (fraction of R_nir) at which the 1/R^2 fall-off reaches 1, and its cap near the vehicle */
  nirRefFrac: 0.5,
  nirGainMax: 1.5,
  /** emitter slots of the thermal view (targets nearest first, then other vehicles) */
  maxEmitters: 16,
  /** LWIR automatic gain: minimum span (K), margins below the background and above the hottest visible emitter */
  agcSpanMinK: 14,
  agcBelowK: 7,
  agcAboveK: 3,
  /** time constant of the gain range (s) */
  agcTauS: 0.6,
  /** LWIR digital zoom range (display only, AWR-04 §6.1) */
  digitalZoomMax: 4,
  /** silhouette floor of a distant emitter (CSS px half size): a hot point still shows */
  emitterMinHalfPx: 1.2,
} as const

export const clamp = (x: number, lo: number, hi: number): number => (x < lo ? lo : x > hi ? hi : x)

/** M19 passive k_light (AWR-04 §6.2): clamp(lg(E / E_min) / 2, 0, 1) */
export function kLight(eLx: number, eMinLx: number): number {
  return clamp(Math.log10(Math.max(eLx, 1e-9) / eMinLx) / 2, 0, 1)
}

/** EO view at illuminance E: gain (dark at night), colour saturation (mono below 0.5 lx), luma noise amplitude */
export function eoLook(eLx: number, out: { gain: number; sat: number; noise: number }): typeof out {
  const kc = kLight(eLx, SV.eMinColorLx)
  const km = kLight(eLx, SV.eMinMonoLx)
  out.gain = 0.04 + 0.96 * km
  out.sat = clamp(2 * kc, 0, 1)
  out.noise = 0.02 + 0.5 * (1 - km)
  return out
}

/** night view: passive mono factor (0.05 lx curve) */
export function nirPassive(eLx: number): number {
  return kLight(eLx, SV.eMinMonoLx)
}

/** active illumination at range r (m) and off-axis angle a (rad) of an illuminator (full divergence theta, range R_nir) */
export function nirActive(r: number, a: number, thetaRad: number, rNirM: number): number {
  if (!(rNirM > 0) || !(thetaRad > 0)) return 0
  const h = thetaRad / 2
  const lo = h * (1 - SV.coneSoft)
  const hi = h * (1 + SV.coneSoft)
  const cone = a <= lo ? 1 : a >= hi ? 0 : 1 - smooth((a - lo) / (hi - lo))
  const edge = clamp((rNirM - r) / (SV.nirEdgeFrac * rNirM), 0, 1)
  const ref = SV.nirRefFrac * rNirM
  const fall = Math.min(SV.nirGainMax, (ref * ref) / Math.max(1, r * r))
  return cone * edge * fall
}

function smooth(t: number): number {
  const x = clamp(t, 0, 1)
  return x * x * (3 - 2 * x)
}

/** illuminator of the payload (uav/{id}/payload, M13): built-in (divergence = HFOV) or external nir_ill_l */
export interface Illuminator { on: boolean; divergenceRad: number; rangeM: number }

/** apparent radiometric temperature of a surface (emissivity eps, kinetic temperature T) against the sky, before the path */
export function surfaceApparentC(tC: number, eps: number, tSkyC: number): number {
  const e = clamp(eps, 0, 1)
  return e * tC + (1 - e) * tSkyC
}

/** path to the sensor: the apparent temperature relaxes towards the air temperature with tau = exp(-sigma R) */
export function pathApparentC(tC: number, tAirC: number, sigmaPerM: number, rM: number): number {
  const tau = Math.exp(-Math.max(0, sigmaPerM) * Math.max(0, rM))
  return tAirC + tau * (tC - tAirC)
}

/** LWIR gain range [lo, hi] (deg C) from the background and the hottest visible emitter (instantaneous target) */
export function lwirRange(tBgC: number, hotC: number, out: Float64Array): Float64Array {
  const lo = tBgC - SV.agcBelowK
  const hi = Math.max(tBgC + SV.agcSpanMinK - SV.agcBelowK, Number.isFinite(hotC) ? hotC + SV.agcAboveK : -Infinity)
  out[0] = lo
  out[1] = Math.max(hi, lo + SV.agcSpanMinK)
  return out
}

/** first-order follow of the gain range (dt s) */
export function followRange(cur: Float64Array, target: Float64Array, dtS: number): void {
  const k = 1 - Math.exp(-Math.max(0, dtS) / SV.agcTauS)
  if (!Number.isFinite(cur[0]) || !Number.isFinite(cur[1])) {
    cur[0] = target[0]
    cur[1] = target[1]
    return
  }
  cur[0] += (target[0] - cur[0]) * k
  cur[1] += (target[1] - cur[1]) * k
}

/** grey palette (scene token ramp pcRamp, linear sRGB): t in [0, 1] -> out; black_hot inverts */
export function paletteRgb(t: number, palette: LwirPalette, out: Float64Array): Float64Array {
  const R = SCENE.pcRamp
  const x = clamp(palette === 'black_hot' ? 1 - t : t, 0, 1) * (R.length - 1)
  const i = Math.min(R.length - 2, Math.floor(x))
  const f = x - i
  for (let k = 0; k < 3; k++) out[k] = R[i][k] + (R[i + 1][k] - R[i][k]) * f
  return out
}

/** the redness test of D2-AC-25 / UX-AC-062 (sRGB 8-bit): a clearly red pixel */
export function isRedPixel(r: number, g: number, b: number): boolean {
  return r >= 96 && r - Math.max(g, b) >= 48
}

/** modes reachable with the focus vehicle's sensors: camera kinds give FPV, EO and night; a thermal sensor gives LWIR */
export function availableModes(hasCamera: boolean, hasThermal: boolean): Record<SensorViewMode, boolean> {
  return { fpv: hasCamera, eo: hasCamera, nir: hasCamera, lwir: hasThermal }
}

/** next mode of the B key cycle FPV -> EO -> night -> LWIR -> FPV, skipping the unavailable ones */
export function nextMode(cur: SensorViewMode, avail: Record<SensorViewMode, boolean>): SensorViewMode {
  const i = SENSOR_VIEW_MODES.indexOf(cur)
  for (let k = 1; k <= SENSOR_VIEW_MODES.length; k++) {
    const m = SENSOR_VIEW_MODES[(i + k) % SENSOR_VIEW_MODES.length]
    if (avail[m]) return m
  }
  return cur
}
