// L0 wind profile f(z_agl) (M07-FR-009; M07 §6.3.6; g06 §5.2; WindNinja windProfile.cpp L73-76). Owner: M07.
// Mirror of python/awr/environment/wind/profile.py (golden profile.json). log: 0 for z <= d + z0, else
// ln((z - d)/z0) / ln((z_ref - d)/z0); power: (max(z, 0)/z_ref)^alpha; uniform: 1 for z > 0.
import type { ProfileCfg } from '../state/presets'

export function profile(zAgl: number, kind: string, zRef: number, z0: number, d: number, alpha: number): number {
  if (kind === 'uniform') return zAgl > 0 ? 1.0 : 0.0
  if (kind === 'power') return (Math.max(zAgl, 0.0) / zRef) ** alpha
  if (zAgl <= d + z0) return 0.0
  return Math.log((zAgl - d) / z0) / Math.log((zRef - d) / z0)
}

export function profileCfg(zAgl: number, c: ProfileCfg): number {
  return profile(zAgl, c.kind, c.z_ref_m, c.z0_m, c.d_m, c.alpha)
}

/** convective speed factor of fronts and the turbulence box (Taylor): f(adv_height_m) */
export function fAdv(c: ProfileCfg): number {
  return profileCfg(c.adv_height_m, c)
}
