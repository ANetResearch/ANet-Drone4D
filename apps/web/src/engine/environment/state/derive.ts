// Derived quantities derive(s) (M07-FR-005; M07 §6.3.3; g06 §4.1; ADR-023). Owner: M07.
// Line-for-line mirror of python/awr/environment/weather/derive.py; every constant comes from presets.json (the model).
// Visibility has one truth, MOR: sigma = ln20 / MOR; mor_bg_m excludes precipitation, precipitation extinction is added,
// mor_m is the headline number (total MOR at the ground). No allocation: derive writes into a caller-owned object.
import { F, PRESETS_MODEL, type PresetsModel, type ProfileCfg } from './presets'
import { profile } from '../wind/profile'

export interface EnvDerived {
  rain_eff_mmh: number
  snow_eff_mmh: number
  sigma_rain: number
  sigma_snow: number
  sigma_precip: number
  sigma_bg: number
  sigma_fog: number
  sigma_haze0: number
  sigma_ground: number
  mor_m: number
  rain_k: number
  snow_k: number
  sun_vis: number
  mp_lambda: number
  cloud_od: number
  v_rain_mps: number
  v_snow_mps: number
  wet_target: number
  vmax_vis_mps: number
}

export const DERIVED_KEYS: readonly (keyof EnvDerived)[] = ['rain_eff_mmh', 'snow_eff_mmh', 'sigma_rain', 'sigma_snow', 'sigma_precip', 'sigma_bg',
  'sigma_fog', 'sigma_haze0', 'sigma_ground', 'mor_m', 'rain_k', 'snow_k', 'sun_vis', 'mp_lambda', 'cloud_od', 'v_rain_mps', 'v_snow_mps',
  'wet_target', 'vmax_vis_mps']

export function newDerived(): EnvDerived {
  return { rain_eff_mmh: 0, snow_eff_mmh: 0, sigma_rain: 0, sigma_snow: 0, sigma_precip: 0, sigma_bg: 0, sigma_fog: 0, sigma_haze0: 0, sigma_ground: 0,
    mor_m: 0, rain_k: 0, snow_k: 0, sun_vis: 0, mp_lambda: 0, cloud_od: 0, v_rain_mps: 0, v_snow_mps: 0, wet_target: 0, vmax_vis_mps: 0 }
}

export function smoothstep(a: number, b: number, x: number): number {
  const t = Math.min(Math.max((x - a) / (b - a), 0.0), 1.0)
  return t * t * (3.0 - 2.0 * t)
}

export function clamp(x: number, a: number, b: number): number {
  return Math.min(Math.max(x, a), b)
}

const LOG51 = Math.log(51.0)

export function derive(s: ArrayLike<number>, d: EnvDerived, prof: ProfileCfg | null = null, P: PresetsModel = PRESETS_MODEL): EnvDerived {
  const c = P.c
  const kMor = c.k_mor
  const gate = smoothstep(P.precipGate[0], P.precipGate[1], s[F.COVER])
  const R = s[F.RAIN] * gate
  const S = s[F.SNOW] * gate
  d.rain_eff_mmh = R
  d.snow_eff_mmh = S
  d.sigma_rain = R > 0 ? c.rain_sigma_coef * R ** c.rain_sigma_exp : 0.0
  d.sigma_snow = S > 0 ? c.snow_sigma_coef * S ** c.snow_sigma_exp : 0.0
  d.sigma_precip = d.sigma_rain + d.sigma_snow
  d.sigma_bg = kMor / clamp(s[F.MOR_BG], c.mor_bg_min_m, c.mor_bg_max_m)
  d.sigma_fog = s[F.FOG_TOP] > 0 ? c.fog_fraction * d.sigma_bg : 0.0
  d.sigma_haze0 = d.sigma_bg - d.sigma_fog
  d.sigma_ground = d.sigma_haze0 + d.sigma_fog + d.sigma_precip
  d.mor_m = kMor / d.sigma_ground
  d.rain_k = clamp(Math.log1p(R) / LOG51, 0.0, 1.0)
  d.snow_k = clamp(Math.log1p(10.0 * S) / LOG51, 0.0, 1.0)
  d.sun_vis = (1.0 - 0.9 * s[F.COVER] ** 1.5) * (1.0 - 0.6 * s[F.DUST])
  d.mp_lambda = 4.1 * Math.max(R, 0.1) ** -0.21
  d.cloud_od = clamp((s[F.TOP] - s[F.BASE]) * 0.022 * 0.35, 0.0, 6.0)
  d.v_rain_mps = 9.65 - 10.3 * Math.exp(-0.6 * clamp(4.0 / d.mp_lambda, 0.3, 5.0))
  d.v_snow_mps = 0.6 + 0.9 * d.snow_k
  d.wet_target = clamp(c.wet_gain * d.rain_k, 0.0, 1.0)
  const pr = prof ?? P.defaultProfile
  d.vmax_vis_mps = s[F.SPEED_REF] * profile(150.0, pr.kind, pr.z_ref_m, pr.z0_m, pr.d_m, pr.alpha) + s[F.GUST_AMP] + 3.0 * s[F.SIGMA_REF] + Math.abs(s[F.W_MEAN])
  return d
}

export function puddleTarget(d: EnvDerived, P: PresetsModel = PRESETS_MODEL): number {
  return d.wet_target ** P.c.puddle_exp
}
