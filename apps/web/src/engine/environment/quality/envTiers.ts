// Environment visual tiers and named constants (M07 §6.4 "T" rows, §6.8.1; r16 §3.1.4, §3.2, §3.9, §3.10). Owner: M07.
// Physical and visual constants of the Low/Med effects (not motion durations: the shutter time of the rain streak and
// the humidity time constants are properties of the simulated scene). Budgets per render tier: Tier S shares 2000 quad
// equivalents between precipitation and wind arrows (1 arrow = 1 equivalent), environment draws <= 3.
export type EnvLevel = 'off' | 'low' | 'med'

export const ENV_TIERS = {
  /** quad equivalents shared by precipitation and arrows on Tier S */
  tierSQuads: 2000,
  /** Low caps by device class: rain quads, snow and dust points */
  low: { software: { rain: 2000, snow: 2000, dust: 2000 }, iGPU: { rain: 8000, snow: 3000, dust: 3000 }, dGPU: { rain: 8000, snow: 3000, dust: 3000 } },
  /** Med caps (D1-ext): B and A */
  med: { B: { rain: 20000, snow: 15000, dust: 16000 }, A: { rain: 60000, snow: 15000, dust: 16000 } },
  maxEnvDrawsS: 3,
  // precipitation box octaves R / H (m), P_MAX and HGT_MAX are multiples of every octave
  precipR: [20, 60, 180, 540] as const,
  precipH: [20, 48, 120, 320] as const,
  precipPMax: 1080,
  precipHgtMax: 960,
  anchorAglLo: 80,
  anchorAglHi: 400,
  hystDown: 0.8,
  hystUp: 1.25,
  anchorFocusFrac: 0.35,
  fallRatios: [0.55, 0.75, 0.9, 1.0] as const,
  dropMinMm: 0.3,
  dropMaxMm: 5,
  /** rain streak exposure (s) and length clamp (m): a physical shutter, not a motion token */
  streakExposureS: 0.042,
  streakMinM: 0.3,
  streakMaxM: 1.2,
  subpixel: 1.15,
  nearCullM: 3,
  nearFadeM: [0.9, 2.6] as const,
  snowPx: [2, 3] as const,
  dustPx: [1, 2] as const,
  snowFlutterM: [2, 0.5] as const,
  // clouds and shadows
  cloudContrast: 1.6,
  cloudHmidFrac: 0.35,
  shadowMaxStrength: 0.8,
  skyFogM: 20000,
  // wind arrows
  arrowGrid: 24,
  arrowSlicesM: [10, 50, 120] as const,
  arrowSpacingM: [10, 20, 40, 80, 160] as const,
  arrowShaftPx: 1.5,
  arrowHeadPx: 6,
  arrowHaloPx: 1,
  arrowOpacity: 0.8,
  arrowLenFrac: 0.9,
  // render order band 60 (M06 transparent band): dust < rain < snow < arrows < streamlines
  order: { cloud2d: -999, dust: 60.1, rain: 60.2, snow: 60.3, arrows: 60.4, streamlines: 60.5 },
} as const

export function arrowCount(): number {
  return ENV_TIERS.arrowGrid * ENV_TIERS.arrowGrid
}
