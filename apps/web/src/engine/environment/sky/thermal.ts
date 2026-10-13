// LWIR thermal background (AWR-04 §6.2; ADR-101; M18-M19 PRD §6.11). Owner: M07 (WP-09).
// Mirror of python/awr/environment/sky/thermal.py (engineering approximation, parameters in docs/impl/D2-WP-09-报告.md):
// air temperature with a diurnal cosine over the true solar time, surface temperature by surface class (emissivity,
// solar gain, night cooling), 8-14 um effective sky temperature, thermal crossover k_cross (0.5 within 1 h after
// sunrise and 1 h before sunset, sunrise and sunset at geometric elevation -0.833 deg).

export const H_RISE_DEG = -0.833
export const K_CROSS_LOW = 0.5

export interface SurfaceClass { eps: number; kSolK: number; kNightK: number }
export const SURFACE_CLASSES: Readonly<Record<string, SurfaceClass>> = {
  urban: { eps: 0.93, kSolK: 15.0, kNightK: 2.5 },
  vegetation: { eps: 0.97, kSolK: 6.0, kNightK: 3.5 },
  soil: { eps: 0.93, kSolK: 12.0, kNightK: 4.0 },
  water: { eps: 0.98, kSolK: 1.0, kNightK: 0.5 },
}

const clamp01 = (x: number): number => Math.min(1, Math.max(0, x))

export function tMeanC(hMslM: number, isaDtC: number): number {
  return 15.0 - 0.0065 * hMslM + isaDtC
}

export function airTempC(tMean: number, tstMin: number, cover: number): number {
  const amp = 4.0 * (1.0 - 0.6 * clamp01(cover))
  return tMean + amp * Math.cos((2.0 * Math.PI * (tstMin / 60.0 - 15.0)) / 24.0)
}

export function surfaceTempC(tAir: number, elDeg: number, cover: number, wetness: number, cls: SurfaceClass): number {
  const c = clamp01(cover)
  const w = clamp01(wetness)
  const s = Math.max(0.0, Math.sin((elDeg * Math.PI) / 180))
  const n = clamp01((6.0 - elDeg) / 12.0)
  return tAir + cls.kSolK * s * (1.0 - 0.7 * c) * (1.0 - 0.5 * w) - cls.kNightK * n * (1.0 - 0.8 * c)
}

export function skyTempC(tAir: number, rh: number, cover: number, cloudBaseM: number): number {
  const c = clamp01(cover)
  const clear = tAir - (40.0 - 20.0 * clamp01(rh))
  const cloud = tAir - 0.0065 * Math.max(0.0, cloudBaseM)
  return (1.0 - c) * clear + c * cloud
}

export function kCrossOf(elNow: number, elBefore: number, elAfter: number): number {
  return elNow >= H_RISE_DEG && (elBefore < H_RISE_DEG || elAfter < H_RISE_DEG) ? K_CROSS_LOW : 1.0
}
