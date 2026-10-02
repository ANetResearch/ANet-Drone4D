// Scene shading provider (M07 §7.2, M07-FR-035; M06 §2.3; AWR-15 §10.3, §10.9; AWR-03 §6.3 M06 row). Owner: M06 (FX-WEB1).
// Environment lighting, fog and sky reach the point material (M05), the mesh materials and the sky (M06) through one
// TSL provider, so M05 and M06 never import M07 (no M07 -> M05 edge, AWR-03 §6.2):
//   lambert(nW, posW)  15 §10.3 light term (float): 0.45 + 0.15 (0.5 + 0.5 n'_up) + 0.55 max(n'.L, 0), n' faced to the eye;
//                      the environment multiplies the direct term by sun visibility and the per-vertex cloud shadow
//   fogFactor(posW)    1 - T along camera -> posW (the environment also sets scene.fogNode = fog(fogColor, fogFactor) once)
//   fogColor           linear sRGB, the horizon colour
//   sky(dirW)          sky colour of a unit view direction (three world frame): gradient, 2D clouds and sky fog
// Without an environment the identity provider below serves: the same light formula with the default sun, no fog, and the
// g950 -> horizon gradient of the scene tokens (skyControl.setHorizon writes its horizon/zenith uniforms). The provider
// is set once by the environment adapter before the shader zoo (it mounts before the other layers; consumers built
// earlier rebuild through onSceneShading, still before the first compile), and a remounted canvas sets its new one.
// Every call builds its own uniform nodes: uniform nodes are never shared between materials on the classic handler.
import { Vector3 } from 'three'
import { Fn, cameraPosition, cameraProjectionMatrixInverse, cameraWorldMatrix, clamp, dot, faceForward, float, max, mix, normalize, pow, uniform, vec4, workingToColorSpace } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'

type N = any // TSL nodes

export interface SceneShadingProvider {
  /** 15 §10.3 light term (float); nW (unit normal) and posW in the three world frame */
  lambert(nW: N, posW: N): N
  /** fog factor 1 - T (float) */
  fogFactor(posW: N): N
  /** fog colour (vec3, linear sRGB, = horizon) */
  readonly fogColor: N
  /** sky colour (vec3, linear sRGB); dirW is a unit direction in the three world frame */
  sky(dirW: N): N
}

/** default sun of the identity provider: ENU (0.3, -0.55, 0.77) in the three frame (E, U, -N) */
export const DEFAULT_SUN_THREE = new Vector3(0.3, 0.77, 0.55).normalize()
export const LIGHT = { ambient: 0.45, sun: 0.55, sky: 0.15, skyGamma: 0.45 } as const

/** the 15 §10.3 formula for a sun direction node (three frame) */
export function lambertFormula(nW: N, posW: N, sunThree: N, direct: N = float(1)): N {
  const V: N = normalize(cameraPosition.sub(posW))
  const nf: N = faceForward(nW, V.negate(), nW)
  return float(LIGHT.ambient).add(nf.y.mul(0.5).add(0.5).mul(LIGHT.sky)).add(max(dot(nf, sunThree), float(0)).mul(direct).mul(LIGHT.sun))
}

/** unit view ray (three world frame) of a clip-space xy for the camera the pass is rendered with */
export function viewDirOfClip(clipXY: N): N {
  const v: N = cameraProjectionMatrixInverse.mul(vec4(clipXY, 1, 1))
  return normalize(cameraWorldMatrix.mul(vec4(v.xyz.div(v.w), 0)).xyz)
}

// identity sky colours, shared by every identity sky() call (each call owns its uniform nodes)
const skyZenith = new Vector3(...SCENE.skyZenith)
const skyHorizon = new Vector3(...SCENE.skyHorizon)

function identityProvider(): SceneShadingProvider {
  return {
    lambert: (nW, posW) => lambertFormula(nW, posW, uniform(DEFAULT_SUN_THREE.clone())),
    fogFactor: () => float(0),
    get fogColor(): N {
      return uniform(skyHorizon)
    },
    sky: (dirW) => {
      const e: N = clamp(dirW.y, float(0), float(1))
      return mix(uniform(skyHorizon), uniform(skyZenith), pow(e, float(LIGHT.skyGamma)))
    },
  }
}

/** identity sky colours (M07 skyControl.setHorizon without a provider); the vectors are read by reference every render */
export function setIdentitySkyColors(horizon: readonly number[], zenith?: readonly number[]): void {
  skyHorizon.set(horizon[0], horizon[1], horizon[2])
  if (zenith) skyZenith.set(zenith[0], zenith[1], zenith[2])
}

const identity = identityProvider()
let current: SceneShadingProvider = identity
let version = 0
const listeners = new Set<() => void>()

/** the scene's provider (the identity one without an environment) */
export function sceneShading(): SceneShadingProvider {
  return current
}
/** increments on every provider change */
export function sceneShadingVersion(): number {
  return version
}
/** true while no environment provider is set */
export function isIdentityShading(): boolean {
  return current === identity
}
/**
 * set the environment provider (before the shader zoo); materials built with the previous provider rebuild through
 * onSceneShading. Returns the function that restores the identity provider (the adapter's unmount).
 */
export function setSceneShading(p: SceneShadingProvider): () => void {
  if (p === current) return () => {}
  current = p
  version++
  for (const l of [...listeners]) l()
  return () => {
    if (current !== p) return
    current = identity
    version++
    for (const l of [...listeners]) l()
  }
}
/** material owners that captured sceneShading() nodes rebuild on a provider change */
export function onSceneShading(cb: () => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

/** sky Fn for a clip-space xy (full-screen quads rendered with the scene camera) */
export const skyOfClip = (clipXY: N): N => Fn(() => sceneShading().sky(viewDirOfClip(clipXY)))()

/** three's token for the renderer's output colour space (ColorSpaceNode resolves it at build time) */
export const OUTPUT_COLOR_SPACE = 'OutputColorSpace'

/**
 * Output transform of a linear colour (vec3) for the pass being built, evaluated where it is called (ADR-064): on the
 * canvas workingToColorSpace(c, renderer.outputColorSpace), as the nodes handler adds per fragment (tone mapping is off,
 * R3F flat); into a render target the colour stays linear (AnetNodesHandler fix 1). three builds separate programs for
 * the canvas and for render targets (output colour space is part of the program key), so each gets its own branch.
 * Materials that call it in the vertex stage set userData.awrOutputInVertex = true: the handler then adds nothing.
 */
export const outputTransform = (rgb: N): N => {
  // a parameterless Fn receives the node builder as its only argument (three r186 TSLCore ShaderCallNodeInternal)
  const f: N = (Fn as N)((builder: { renderer?: { getRenderTarget?(): { isXRRenderTarget?: boolean } | null } }) => {
    const rt = builder.renderer?.getRenderTarget?.() ?? null
    if (rt !== null && rt.isXRRenderTarget !== true) return rgb
    return (workingToColorSpace(vec4(rgb, 1), OUTPUT_COLOR_SPACE) as N).xyz
  })
  return f()
}
