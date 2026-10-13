// Acoustic background profiles (AWR-04 §7.3; M18-FR-008, FR-012; M18-M19 PRD §6.11). Owner: M07 (WP-09).
// Mirror of python/awr/environment/sky/acoustic.py: day/night A-weighted totals rural 40/30, suburban 50/40, urban 60/50
// dB(A), linear in sun elevation between -6 (night) and +6 deg (day), rain boost (5 + 5 rain_k) min(1, R/0.5) dB; 24
// third-octave bands 50 Hz-10 kHz with one A-weighted relative shape per profile normalised to an energy sum of 0 dB.

export const BANDS_HZ = [50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300,
  8000, 10000] as const
export const N_BANDS = BANDS_HZ.length
export const PROFILES = ['rural', 'suburban', 'urban'] as const
export type AcousticProfile = (typeof PROFILES)[number]
export const LEVELS_DBA: Readonly<Record<AcousticProfile, readonly [number, number]>> = { rural: [40, 30], suburban: [50, 40], urban: [60, 50] }
const SHAPE_RAW: Readonly<Record<AcousticProfile, readonly number[]>> = {
  rural: [-30, -28, -26, -24, -22, -20, -18, -16, -14, -13, -12, -11, -11, -11, -11, -11, -11, -12, -13, -14, -16, -18, -21, -24],
  suburban: [-26, -25, -23, -21, -20, -18, -16, -15, -14, -13, -12, -11, -10, -9, -9, -10, -10, -11, -12, -14, -16, -18, -21, -24],
  urban: [-24, -23, -21, -20, -20, -18, -16, -15, -14, -13, -12, -11, -9, -8, -9, -10, -11, -13, -15, -16, -18, -20, -22, -25],
}

/** IEC 61672-1 A-weighting (dB) */
export function aWeightDb(fHz: number): number {
  const f2 = fHz * fHz
  const ra = (12194.0 ** 2 * f2 * f2) / ((f2 + 20.6 ** 2) * Math.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2))
  return 20.0 * Math.log10(ra) + 2.0
}

function normalise(shape: readonly number[]): Float64Array {
  let sum = 0
  for (const x of shape) sum += 10.0 ** (x / 10.0)
  const tot = 10.0 * Math.log10(sum)
  return Float64Array.from(shape, (x) => x - tot)
}

export const REL_SHAPE_DB: Readonly<Record<AcousticProfile, Float64Array>> = {
  rural: normalise(SHAPE_RAW.rural), suburban: normalise(SHAPE_RAW.suburban), urban: normalise(SHAPE_RAW.urban),
}

export function isAcousticProfile(x: unknown): x is AcousticProfile {
  return typeof x === 'string' && (PROFILES as readonly string[]).includes(x)
}

export function rainBoostDb(rainEffMmh: number): number {
  const r = Math.max(0, rainEffMmh)
  if (r <= 0) return 0
  const rainK = Math.min(1, Math.max(0, Math.log1p(r) / Math.log(51.0)))
  return (5.0 + 5.0 * rainK) * Math.min(1, r / 0.5)
}

export function levelDba(profile: AcousticProfile, elDeg: number, rainEffMmh = 0): number {
  const [day, night] = LEVELS_DBA[profile]
  const k = Math.min(1, Math.max(0, (elDeg + 6.0) / 12.0))
  return night + (day - night) * k + rainBoostDb(rainEffMmh)
}

export function bandLevelsDba(profile: AcousticProfile, total: number, out: Float64Array = new Float64Array(N_BANDS)): Float64Array {
  const rel = REL_SHAPE_DB[profile]
  for (let i = 0; i < N_BANDS; i++) out[i] = total + rel[i]
  return out
}

export function totalDba(bands: ArrayLike<number>): number {
  let s = 0
  for (let i = 0; i < bands.length; i++) s += 10.0 ** (bands[i] / 10.0)
  return 10.0 * Math.log10(s)
}
