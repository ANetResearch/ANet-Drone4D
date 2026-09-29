// Environment uniforms (M07 §6.2.8; ADR-007; M06 §6.3). Owner: M07.
// EnvParams holds the CPU values written once per frame by EnvironmentRuntime.update (from EnvStore, the camera and the
// quality level). Every material gets its own uniform nodes (uniform nodes are never shared between materials under
// WebGLNodesHandler, M06 §6.3); each node reads EnvParams in onRenderUpdate (idempotent, once per render), is float or
// float vector only (int uniforms break under the handler, M06 rule 5) and sits in renderGroup. Anything that needs
// float64 (grid anchors, drift offsets, modulo of large positions) is reduced on the CPU first: the GPU only receives
// bounded values (r16 §6 float32 precision).
import { Vector2, Vector3, Vector4 } from 'three'
import { renderGroup, uniform } from 'three/tsl'
import { PALETTE_LINEAR, type PrimitiveName } from '@/lib/tokens/palette.gen'
import { SCENE } from '@/lib/tokens/scene.gen'

type N = any // TSL nodes

export const MAX_GUSTS = 4

export class EnvParams {
  // camera (ENU)
  readonly camEnu = new Vector3()
  pixelWorldScale = 0.001
  readonly viewportPx = new Vector2(1, 1)
  // sun and clouds
  readonly sunDirThree = new Vector3(0, 1, 0)
  readonly sunDirEnu = new Vector3(0, 0, 1)
  sunVis = 1
  cloudCover = 0
  cloudOD = 0
  cloudHmid = 1500
  cloud2DAlphaMax = 0
  readonly cloudOffset = new Vector2()
  weatherScale = 24000
  shadowStrength = 0
  cloudsOn = 1
  // fog (ENU z, AGL from groundZ)
  groundZ = 0
  sigmaHaze0 = 1e-4
  hazeH = 1500
  sigmaFog = 0
  fogTop = 0
  sigmaPrecip = 0
  precipTop = 0
  readonly fogColor = new Vector3(...SCENE.skyHorizon)
  readonly zenithColor = new Vector3(...SCENE.skyZenith)
  // wind (ENU)
  windS = 0
  readonly windE = new Vector2(1, 0)
  wMean = 0
  /** 0 log, 1 power, 2 uniform */
  profKind = 0
  profZ0 = 0.5
  profD = 0
  profZref = 10
  profAlpha = 0.25
  fAdv = 1
  level = 1
  /** (xi0_k, e_k.x, e_k.y, 0) with xi0_k = f_adv S - x0_k + s0_k (float64 on the CPU) */
  readonly gustA = Array.from({ length: MAX_GUSTS }, () => new Vector4())
  /** (amp_k, 2 pi / lam_k, lam_k, active_k) */
  readonly gustB = Array.from({ length: MAX_GUSTS }, () => new Vector4())
  turbOn = 0
  /** (f_adv D) mod 256 */
  readonly turbD = new Vector3()
  turbSigmaRef = 0
  vmax = 20
  // precipitation
  rainK = 0
  snowK = 0
  dustK = 0
  /** Marshall-Palmer slope (1/mm) of the drop size distribution Gamma(4, lambda) */
  rainLambda = 4
  /** raster pixels per CSS pixel (point sizes, arrow widths) */
  dpr = 1
  readonly windOffset = new Vector3()
  readonly fallPhase4 = new Vector4()
  readonly fallSpeed4 = new Vector4()
  snowPhase = 0
  readonly precipAnchor = new Vector3()
  precipR = 20
  precipH = 20
  precipFade = 1
  readonly precipWind = new Vector3()
  // surface
  wetness = 0
  puddle = 0
  // wind arrows
  readonly arrowOrigin = new Vector3()
  arrowSpacing = 40
  arrowSliceAgl = 50
  arrowPx = 8
  // quality and switches (0/1)
  visualOn = 1
}

/** per-material uniform node set bound to one EnvParams */
export interface EnvNodes {
  camEnu: N; pixelWorldScale: N; viewportPx: N
  sunDirThree: N; sunDirEnu: N; sunVis: N; cloudCover: N; cloudOD: N; cloudHmid: N; cloud2DAlphaMax: N; cloudOffset: N; weatherScale: N
  shadowStrength: N; cloudsOn: N
  groundZ: N; sigmaHaze0: N; hazeH: N; sigmaFog: N; fogTop: N; sigmaPrecip: N; precipTop: N; fogColor: N; zenithColor: N
  windS: N; windE: N; wMean: N; profKind: N; profZ0: N; profD: N; profZref: N; profAlpha: N; fAdv: N; level: N
  gustA: N; gustB: N; turbOn: N; turbD: N; turbSigmaRef: N; vmax: N
  rainK: N; snowK: N; dustK: N; rainLambda: N; dpr: N; windOffset: N; fallPhase4: N; fallSpeed4: N; snowPhase: N; precipAnchor: N; precipR: N; precipH: N
  precipFade: N; precipWind: N
  wetness: N; puddle: N
  arrowOrigin: N; arrowSpacing: N; arrowSliceAgl: N; arrowPx: N
  visualOn: N
}

type Key = Exclude<keyof EnvNodes, 'gustA' | 'gustB'>

/** a new node set reading `p` (call once per material) */
export function makeEnvNodes(p: EnvParams): EnvNodes {
  const u = (k: Key): N => {
    const v = (p as unknown as Record<string, unknown>)[k]
    const n: N = uniform(v as never).setGroup(renderGroup)
    return typeof v === 'number' ? n.onRenderUpdate(() => (p as unknown as Record<string, number>)[k]) : n
  }
  const keys: Key[] = ['camEnu', 'pixelWorldScale', 'viewportPx', 'sunDirThree', 'sunDirEnu', 'sunVis', 'cloudCover', 'cloudOD', 'cloudHmid',
    'cloud2DAlphaMax', 'cloudOffset', 'weatherScale', 'shadowStrength', 'cloudsOn', 'groundZ', 'sigmaHaze0', 'hazeH', 'sigmaFog', 'fogTop',
    'sigmaPrecip', 'precipTop', 'fogColor', 'zenithColor', 'windS', 'windE', 'wMean', 'profKind', 'profZ0', 'profD', 'profZref', 'profAlpha',
    'fAdv', 'level', 'turbOn', 'turbD', 'turbSigmaRef', 'vmax', 'rainK', 'snowK', 'dustK', 'rainLambda', 'dpr', 'windOffset', 'fallPhase4', 'fallSpeed4',
    'snowPhase', 'precipAnchor', 'precipR', 'precipH', 'precipFade', 'precipWind', 'wetness', 'puddle', 'arrowOrigin', 'arrowSpacing',
    'arrowSliceAgl', 'arrowPx', 'visualOn']
  const out: Record<string, N> = {}
  for (const k of keys) out[k] = u(k)
  // vector uniforms hold the EnvParams objects themselves (value by reference, uploaded every render); the 4 fronts
  // are 8 separate vec4 uniforms (no uniform arrays on the classic handler path)
  out.gustA = p.gustA.map((v) => uniform(v).setGroup(renderGroup))
  out.gustB = p.gustB.map((v) => uniform(v).setGroup(renderGroup))
  return out as unknown as EnvNodes
}

const STEPS: readonly [number, PrimitiveName][] = [[500, 'g500'], [600, 'g600'], [700, 'g700'], [800, 'g800'], [850, 'g850']]

/** horizon (= fog) colour of visual.horizon_step: linear sRGB interpolation between adjacent Graphite steps (M07 §8.5) */
export function horizonColor(step: number, out: Vector3): Vector3 {
  const s = Math.min(Math.max(step, STEPS[0][0]), STEPS[STEPS.length - 1][0])
  let i = 0
  while (i < STEPS.length - 2 && s > STEPS[i + 1][0]) i++
  const [a0, na] = STEPS[i]
  const [a1, nb] = STEPS[i + 1]
  const k = (s - a0) / (a1 - a0)
  const A = PALETTE_LINEAR[na]
  const B = PALETTE_LINEAR[nb]
  return out.set(A[0] + (B[0] - A[0]) * k, A[1] + (B[1] - A[1]) * k, A[2] + (B[2] - A[2]) * k)
}
