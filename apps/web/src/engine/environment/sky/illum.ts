// Ground illuminance and illuminance band (AWR-04 §6.5; ADR-102). Owner: M07 (WP-09).
// Mirror of python/awr/environment/sky/illum.py: geometric elevation h (deg); h >= 10: 1.2e5 sin h; 0-10: linear down to
// 400 lx; civil twilight (-6..0) log 400 -> 3 lx; nautical (-12..-6) 3 -> 0.008 lx; astronomical (-18..-12) 0.008 -> 0.001
// lx (continuous, monotonic); below 0.001 lx; optional moonlight; cloud cover c multiplies by (1 - 0.7 c).
// Bands (rt/enums.json LightBand): day h >= 6 (0), low -6 <= h < 6 (1), night h < -6 (2).

export const BAND_DAY = 0
export const BAND_LOW = 1
export const BAND_NIGHT = 2
export const BAND_NAMES = ['day', 'low', 'night'] as const
export type BandName = (typeof BAND_NAMES)[number]
export const E_FLOOR_LX = 0.001
export const MOON_FULL_LX = 0.1
const E_SUN = 1.2e5
const E_HORIZON = 400.0
const E10 = E_SUN * Math.sin((10 * Math.PI) / 180)
const LG400 = Math.log10(400.0)
const LG3 = Math.log10(3.0)
const LG008 = Math.log10(0.008)
const LG001 = Math.log10(0.001)

function clearLx(h: number): number {
  if (h >= 10.0) return E_SUN * Math.sin((h * Math.PI) / 180)
  if (h >= 0.0) return E_HORIZON + ((E10 - E_HORIZON) * h) / 10.0
  if (h >= -6.0) return 10.0 ** (LG3 + ((LG400 - LG3) * (h + 6.0)) / 6.0)
  if (h >= -12.0) return 10.0 ** (LG008 + ((LG3 - LG008) * (h + 12.0)) / 6.0)
  if (h >= -18.0) return 10.0 ** (LG001 + ((LG008 - LG001) * (h + 18.0)) / 6.0)
  return E_FLOOR_LX
}

export function illuminanceLx(elDeg: number, cover = 0, moonLx = 0): number {
  const c = Math.min(1, Math.max(0, cover))
  return (clearLx(elDeg) + moonLx) * (1.0 - 0.7 * c)
}

export function illumBand(elDeg: number): 0 | 1 | 2 {
  if (elDeg >= 6.0) return BAND_DAY
  return elDeg >= -6.0 ? BAND_LOW : BAND_NIGHT
}
