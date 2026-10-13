// Optical depth and point extinction, CPU (M07-FR-006, FR-020; M07 §6.3.4, §6.3.12; g06 §4.3, §4.4; ADR-023). Owner: M07.
// Mirror of python/awr/environment/atmosphere/optics.py (golden optical_depth.json): exponential haze (scale height
// H = 1500 m, Quilez closed form) + flat fog layer + uniform precipitation layer below the cloud base; optical layers use
// coordinate.ground.zM as the AGL origin. Web fog and sky use T = exp(-tau) (atmosphere/fogNode.ts, same formula in TSL).
// D2 (WP-09; AWR-04 §6.3 step 4; ADR-101): per-wavelength extinction (aerosol scaled by Kim, precipitation unchanged, LWIR
// >= 8 um adds water vapour (0.03 + 0.012 rho_v) per km, rho_v from relative humidity and the mean air temperature by
// Magnus), for the frontend perception calculator (M19 TS port).
import type { EnvDerived } from '../state/derive'

export const H_HAZE = 1500.0
export const FLAG_IN_FOG_LAYER = 16
export const FLAG_BELOW_CLOUD_PRECIP = 32

export function flatLen(roZ: number, rdZ: number, L: number, top: number): number {
  let t0 = 0.0
  let t1 = L
  if (Math.abs(rdZ) > 1e-5) {
    const tc = (top - roZ) / rdZ
    if (rdZ > 0) t1 = Math.min(t1, tc)
    else t0 = Math.max(t0, tc)
  } else if (roZ > top) return 0.0
  return Math.max(t1 - t0, 0.0)
}

export function opticalDepth(z0Agl: number, rdZ: number, L: number, d: EnvDerived, fogTop: number, cloudBase: number, H = H_HAZE): number {
  const a = d.sigma_haze0 * Math.exp(-z0Agl / H)
  const k = (rdZ * L) / H
  let od = a * L * (Math.abs(k) > 1e-4 ? (1.0 - Math.exp(-k)) / k : 1.0)
  if (d.sigma_fog > 0) od += d.sigma_fog * flatLen(z0Agl, rdZ, L, fogTop)
  if (d.sigma_precip > 0) od += d.sigma_precip * flatLen(z0Agl, rdZ, L, cloudBase)
  return od
}

/** out = [sigma_per_m, flags] */
export function sigmaAt(zAgl: number, d: EnvDerived, fogTop: number, cloudBase: number, out: Float64Array): Float64Array {
  let s = d.sigma_haze0 * Math.exp(-zAgl / H_HAZE)
  let flags = 0
  if (zAgl < fogTop) {
    s += d.sigma_fog
    flags |= FLAG_IN_FOG_LAYER
  }
  if (zAgl < cloudBase) {
    s += d.sigma_precip
    if (d.sigma_precip > 0) flags |= FLAG_BELOW_CLOUD_PRECIP
  }
  out[0] = s
  out[1] = flags
  return out
}

export function kimQ(v2Km: number): number {
  if (v2Km > 50) return 1.6
  if (v2Km > 6) return 1.3
  if (v2Km > 1) return 0.16 * v2Km + 0.34
  if (v2Km > 0.5) return v2Km - 0.5
  return 0.0
}

export const LWIR_MIN_NM = 8000.0

/** water vapour density (g/m3) from relative humidity (0-1) and temperature (degC), Magnus saturation pressure */
export function waterVapourGm3(rh: number, tC: number): number {
  const es = 6.112 * Math.exp((17.62 * tC) / (243.12 + tC))
  return (216.7 * Math.min(1, Math.max(0, rh)) * es) / (tC + 273.15)
}

/** LWIR water vapour absorption (1/m): 0.03 + 0.012 rho_v per km */
export function sigmaWvPerM(rhoVGm3: number): number {
  return (0.03 + 0.012 * Math.max(0, rhoVGm3)) / 1000.0
}

/** aerosol wavelength ratio (lambda / 550)^-q with Kim's q at V2 = ln50 / sigma_bg (km) */
export function kimFactor(d: EnvDerived, lamNm: number, kV2: number): number {
  return (lamNm / 550.0) ** -kimQ(kV2 / d.sigma_bg / 1000.0)
}

export function sigmaLambda(d: EnvDerived, lamNm: number, kV2: number, rhoVGm3 = 0): number {
  const v2Km = kV2 / d.sigma_bg / 1000.0
  let s = d.sigma_bg * (lamNm / 550.0) ** -kimQ(v2Km) + d.sigma_precip
  if (lamNm >= LWIR_MIN_NM) s += sigmaWvPerM(rhoVGm3)
  return s
}

export function lidarTwoWay(d: EnvDerived, lamNm: number, rM: number, kV2: number): number {
  return Math.exp(-2.0 * sigmaLambda(d, lamNm, kV2) * rM)
}
