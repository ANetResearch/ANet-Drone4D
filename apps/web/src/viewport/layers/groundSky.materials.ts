// Ground grid and sky materials, TSL (M06-FR-027; AWR-15 §10.11, §10.9; g01 §5). Owner: M06.
// Sky: the scene shading provider's sky(dir) (engine/shading.ts; M07 environment: gradient to the fog colour, 2D clouds
// and sky fog; FX-WEB1); without an environment the analytic gradient from the zenith (g950) to the horizon colour
// (uniform, skyControl.setHorizon) along the view direction's elevation. Tier B/A evaluate the Fn per pixel in the P2
// composite for pixels whose depth is the far plane. Tier S draws it on SkyQuad (ADR-064, FX2-R2): a 32 x 18 cell
// full-screen grid at the far plane, evaluated per vertex (colour and output transform, userData.awrOutputInVertex) and
// drawn after the opaque objects with the depth test on, so only pixels still at the cleared depth are filled and each
// costs one varying write. Measured on SwiftShader at 640 x 360 (layer pairing, 25k points): the per-pixel full-screen
// sky (first, no depth) about 15 ms; the grid at 64 x 36 cells 6.1 ms, 32 x 18 2.2 ms, 16 x 9 1.6 ms (triangle setup
// dominates); 20 raster px cells keep the environment sky (gradient (1 - up)^3, sky fog, 2D cloud mask) within 1/255.
// Directions come from the camera matrices (uniforms updated once per frame; view offset included through the projection
// inverse). A provider change before the shader zoo rebuilds the sky materials (onSceneShading).
// Ground: procedural grid on the plane z = ground.zM - 0.5 m (layer frame ENU), minor 10 m and major 100 m lines one
// raster pixel wide through fwidth, faded out between 0.35 far and 0.7 far; depth test on, no depth write. The two line
// colours get the output transform in the vertex stage (constant over the plane) and are mixed per fragment, so the
// fragment stage carries no transfer function (ADR-064; the grid differs from the per-fragment encoding only on the
// anti-aliased edge pixels of a major line, where minor and major colours mix).
// All colours are uniforms from lib/tokens/scene.gen.ts (linear sRGB); switching presets never recompiles (D1-AC-25).
import { Matrix4, PlaneGeometry, Vector3, type PerspectiveCamera } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import {
  Fn, abs, cameraPosition, clamp, float, fract, fwidth, max, min, mix, normalize, positionGeometry, positionLocal, positionWorld, pow, smoothstep,
  uniform, varying, vec2, vec4,
} from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { isIdentityShading, outputTransform, sceneShading } from '@/engine'

type N = any // TSL nodes

export const GROUND = { minorM: 10, majorM: 100, belowGroundM: 0.5, fadeFrom: 0.35, fadeTo: 0.7, planeHalfM: 20_000 } as const
/**
 * Tier S SkyQuad (ADR-064): grid cells (20 raster px at 640 x 360), render order after the opaque objects, far-plane
 * depth in NDC (just inside the far plane: the cleared depth passes LessEqual, reversed GreaterEqual, any drawn pixel fails)
 */
export const SKY = { cellsX: 32, cellsY: 18, renderOrder: 1000, farNdc: 0.999999, farNdcReversed: 1e-6 } as const

export interface SkyUniforms { zenith: N; horizon: N; camWorld: N; projInv: N }

/** per-material sky uniforms (uniform nodes are never shared between materials under WebGLNodesHandler) */
export function makeSkyUniforms(): SkyUniforms {
  const z = SCENE.skyZenith
  const h = SCENE.skyHorizon
  return { zenith: uniform(new Vector3(z[0], z[1], z[2])), horizon: uniform(new Vector3(h[0], h[1], h[2])), camWorld: uniform(new Matrix4()), projInv: uniform(new Matrix4()) }
}

/** per frame: camera matrices (projection inverse includes the view offset) */
export function updateSkyUniforms(u: SkyUniforms, cam: PerspectiveCamera): void {
  ;(u.camWorld.value as Matrix4).copy(cam.matrixWorld)
  ;(u.projInv.value as Matrix4).copy(cam.projectionMatrixInverse)
}
export function setSkyColors(u: SkyUniforms, horizon: readonly number[], zenith?: readonly number[]): void {
  ;(u.horizon.value as Vector3).set(horizon[0], horizon[1], horizon[2])
  if (zenith) (u.zenith.value as Vector3).set(zenith[0], zenith[1], zenith[2])
}

/** sky colour for a clip-space xy (TSL Fn body) */
export function skyColor(u: SkyUniforms, ndcXY: N): N {
  const v: N = u.projInv.mul(vec4(ndcXY, 1, 1))
  const dirView: N = v.xyz.div(v.w)
  const dirWorld: N = normalize(u.camWorld.mul(vec4(dirView, 0)).xyz)
  if (!isIdentityShading()) return sceneShading().sky(dirWorld)
  const e: N = clamp(dirWorld.y, float(0), float(1))
  return mix(u.horizon, u.zenith, pow(e, float(0.45)))
}

/** Tier S SkyQuad geometry: the full-screen grid of SKY.cellsX x SKY.cellsY cells (clip-space xy in [-1, 1]) */
export function makeSkyQuadGeometry(): PlaneGeometry {
  return new PlaneGeometry(2, 2, SKY.cellsX, SKY.cellsY)
}

/**
 * Tier S SkyQuad material (ADR-064): sky colour and output transform per vertex of the makeSkyQuadGeometry() grid, at
 * the far plane with the depth test on and no depth write; the mesh renders after the opaque objects (SKY.renderOrder)
 */
export function makeSkyQuadMaterial(u: SkyUniforms, reversedZ: boolean): MeshBasicNodeMaterial {
  const m = new MeshBasicNodeMaterial()
  const xy: N = (positionGeometry as N).xy
  m.vertexNode = vec4(xy, reversedZ ? SKY.farNdcReversed : SKY.farNdc, 1) as N
  const sky: N = Fn(() => outputTransform(skyColor(u, (positionGeometry as N).xy)))()
  m.colorNode = vec4(varying(sky, 'vSkyColor') as N, 1) as N
  m.userData.awrOutputInVertex = true
  m.depthTest = true
  m.depthWrite = false
  m.fog = false
  return m
}

export interface GridUniforms { minor: N; major: N; minorA: N; majorA: N; far: N }
export function makeGridUniforms(): GridUniforms {
  const a = SCENE.gridMinor
  const b = SCENE.gridMajor
  return {
    minor: uniform(new Vector3(a.rgb[0], a.rgb[1], a.rgb[2])), major: uniform(new Vector3(b.rgb[0], b.rgb[1], b.rgb[2])),
    minorA: uniform(a.a), majorA: uniform(b.a), far: uniform(20_000),
  }
}

/** ground grid on a large plane in the layer frame (x east, y north) */
export function makeGridMaterial(u: GridUniforms): MeshBasicNodeMaterial {
  const m = new MeshBasicNodeMaterial()
  const line = (p: N, cell: number): N => {
    const q: N = p.div(cell)
    const w: N = max(fwidth(q), vec2(1e-6, 1e-6))
    const g: N = abs(fract(q.sub(0.5)).sub(0.5)).div(w)
    return float(1).sub(min(min(g.x, g.y), float(1)))
  }
  const alpha = Fn(() => {
    const p: N = (positionLocal as N).xy
    const lMinor: N = line(p, GROUND.minorM)
    const lMajor: N = line(p, GROUND.majorM)
    const d: N = (positionWorld as N).sub(cameraPosition).length()
    const fade: N = float(1).sub(smoothstep(u.far.mul(GROUND.fadeFrom), u.far.mul(GROUND.fadeTo), d))
    return max(lMinor.mul(u.minorA), lMajor.mul(u.majorA)).mul(fade)
  })
  const minorOut: N = varying(outputTransform(u.minor), 'vGridMinor')
  const majorOut: N = varying(outputTransform(u.major), 'vGridMajor')
  const color = Fn(() => {
    const p: N = (positionLocal as N).xy
    return mix(minorOut, majorOut, line(p, GROUND.majorM))
  })
  m.colorNode = color() as N
  m.opacityNode = alpha() as N
  m.userData.awrOutputInVertex = true
  m.transparent = true
  m.depthTest = true
  m.depthWrite = false
  m.fog = false
  return m
}

