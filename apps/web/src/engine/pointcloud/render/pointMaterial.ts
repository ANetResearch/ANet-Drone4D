// Point material, TSL single source (ADR-005, ADR-007, ADR-010, ADR-011; M05 §6.7.2-§6.7.5, M05-FR-028..034). Owner: M05.
// One program per backend tier: attribute-less Points pulled through the DrawTable (fetchNode.ts); Lite point size with
// childDrawnMask (one level smaller where the child octant is drawn and faded in), adaptive maxPx; node-local Weyl fade
// hash; class mask; colour modes selected by the float uniform uColorMode inside uniform branches (Height 0, HAG 1,
// Normal 2, Class 3, Source 4, Intensity 5) so switching never compiles; lighting per vertex from the scene shading
// provider (engine/shading.ts; M07 §7.2): lambert(n_world, p_world) on the oct16 normal = 0.45 + 0.15 (0.5 + 0.5 n'_up)
// + 0.55 max(n'.L, 0) with the environment's sun visibility and cloud shadow on the direct term only (AWR-15 §10.3;
// FX-WEB1: replaces base·lam·cloudShadow). Hidden points (class off, fade hash) get size 0 and a position behind the eye.
// Tier S draws square points (no fragment discard); Tier B/A round points (discard outside the unit disc), fixed when the
// material is built. Every integer parameter is a float uniform; colours are linear, written once from the scene tokens.
// Per-vertex output (ADR-064, FX2-R2): a GL point carries the varyings of its single vertex over the whole sprite, so the
// scene fog mix(c, fogColor, fogFactor(p)) (the provider's terms, identical to scene.fogNode) and the output colour
// transform (engine/shading.ts outputTransform: sRGB on the canvas, linear into the Tier B/A cloud target) are evaluated in
// the vertex stage with the same result as per fragment; the fragment stage only writes vPcColor (plus the disc discard
// on Tier B/A). The material sets fog = false and userData.awrOutputInVertex, which tells the nodes handler not to add
// the output transform again (viewport/anetNodesHandler.ts). On SwiftShader the per-fragment fog (3 exp) and sRGB OETF
// (3 pow) made the 25k-point pass cost 35 ms of raster; per vertex it costs 19 ms.
import { DataTexture, FloatType, NearestFilter, RGBAFormat, RedFormat, Vector2, Vector3 } from 'three'
import type { PointsNodeMaterial } from 'three/webgpu'
import {
  Discard, Fn, If, abs, builtin, clamp, float, floor, fract, int, ivec2, length, max, min, mix, modelViewMatrix, modelWorldMatrix, normalize, pointUV, pow,
  select, texture, uint, uniform, varyingProperty, vec3, vec4,
} from 'three/tsl'
import type { PointSizeMode } from '../../loop'
import { outputTransform, type SceneShadingProvider } from '../../shading'
import { PC } from '../params'
import { fetchPoint, type N } from './fetchNode'

export const COLOR_MODE_INDEX = { height: 0, hag: 1, normal: 2, class: 3, source: 4, intensity: 5 } as const

export interface PointColorTokens {
  /** --pc-ramp-0..4, linear */
  ramp: readonly (readonly [number, number, number])[]
  /** class colours 0..15, linear */
  classColors: readonly (readonly [number, number, number])[]
  /** Normal mode base colour (g300), linear */
  normalBase: readonly [number, number, number]
  /** class 11 colour when a higher-priority red entity is on screen (g50), linear */
  heroDemoted: readonly [number, number, number]
  gamma: number
}

export interface PointUniforms {
  numDraws: N
  sizeK: N
  projK: N
  minPx: N
  /** size cap of non-leaf nodes (ADR-063: the tau cap in dense frames, maxPxSparse in sparse frames) */
  maxPx: N
  /** size cap of leaf nodes (no child in the data): the rung maxPx in dense frames, maxPxSparse in sparse frames */
  maxPxLeaf: N
  classMask: N
  colorMode: N
  heroActive: N
  zLo: N
  zHi: N
  hagLo: N
  hagHi: N
  gamma: N
  eye: N
  behind: N
  sun: N
  cloudShadow: N
  dtmReady: N
  dtmOrigin: N
  dtmCell: N
  dtmSize: N
  groundZ: N
  normalBase: N
  heroDemoted: N
  ramp: N[]
}

export interface PointTextures {
  /** texture(...) base nodes, see FetchTextures */
  pool: N
  draw: N
  /** DrawTable block index (FetchTextures.block) */
  block: N
  node: N
  dtm: N
  classes: N
}

export function makePointUniforms(tok: PointColorTokens): PointUniforms {
  return {
    numDraws: uniform(0), sizeK: uniform(PC.sizeK), projK: uniform(1), minPx: uniform(2), maxPx: uniform(8), maxPxLeaf: uniform(8), classMask: uniform(PC.classMaskDefault),
    colorMode: uniform(0), heroActive: uniform(0), zLo: uniform(0), zHi: uniform(1), hagLo: uniform(0), hagHi: uniform(1), gamma: uniform(tok.gamma),
    eye: uniform(new Vector3()), behind: uniform(new Vector3(0, 0, -1e6)), sun: uniform(new Vector3(0.3, -0.55, 0.77).normalize()), cloudShadow: uniform(1),
    dtmReady: uniform(0), dtmOrigin: uniform(new Vector2()), dtmCell: uniform(10), dtmSize: uniform(new Vector2(1, 1)), groundZ: uniform(0),
    normalBase: uniform(new Vector3(...tok.normalBase)), heroDemoted: uniform(new Vector3(...tok.heroDemoted)),
    ramp: tok.ramp.map((c) => uniform(new Vector3(c[0], c[1], c[2]))),
  }
}

/** 16 x 1 RGBA32F class colour table (linear) */
export function makeClassTexture(colors: readonly (readonly [number, number, number])[]): DataTexture {
  const d = new Float32Array(16 * 4)
  for (let i = 0; i < 16; i++) {
    const c = colors[i] ?? colors[0]
    d.set([c[0], c[1], c[2], 1], 4 * i)
  }
  const t = new DataTexture(d, 16, 1, RGBAFormat, FloatType)
  t.minFilter = NearestFilter
  t.magFilter = NearestFilter
  t.generateMipmaps = false
  t.needsUpdate = true
  return t
}

/** 1 x 1 R32F stand-in for the DTM before it is loaded */
export function dummyDtmTexture(): DataTexture {
  const t = new DataTexture(new Float32Array(1), 1, 1, RedFormat, FloatType)
  t.minFilter = NearestFilter
  t.magFilter = NearestFilter
  t.generateMipmaps = false
  t.needsUpdate = true
  return t
}

/** five-stop ramp, t in [0, 1] */
function ramp5(u: PointUniforms, t: N): N {
  const s: N = t.mul(4)
  let c: N = mix(u.ramp[0], u.ramp[1], clamp(s, float(0), float(1)))
  c = mix(c, u.ramp[2], clamp(s.sub(1), float(0), float(1)))
  c = mix(c, u.ramp[3], clamp(s.sub(2), float(0), float(1)))
  c = mix(c, u.ramp[4], clamp(s.sub(3), float(0), float(1)))
  return c
}

/** DTM (stored as dtm - ground.zM) at layer (x, y): cell-centre bilinear, clamped at the border (AWR-16 §6.2) */
function dtmAt(u: PointUniforms, tex: PointTextures, x: N, y: N): N {
  const W: N = u.dtmSize.x
  const H: N = u.dtmSize.y
  const gx: N = clamp(x.sub(u.dtmOrigin.x).div(u.dtmCell).sub(0.5), float(0), max(W.sub(1), float(0))).toVar()
  const gy: N = clamp(y.sub(u.dtmOrigin.y).div(u.dtmCell).sub(0.5), float(0), max(H.sub(1), float(0))).toVar()
  const c0: N = min(floor(gx), max(W.sub(2), float(0))).toVar()
  const r0: N = min(floor(gy), max(H.sub(2), float(0))).toVar()
  const tx: N = gx.sub(c0)
  const ty: N = gy.sub(r0)
  const c1: N = min(c0.add(1), W.sub(1))
  const r1: N = min(r0.add(1), H.sub(1))
  const v00: N = tex.dtm.load(ivec2(int(c0), int(r0))).x
  const v01: N = tex.dtm.load(ivec2(int(c1), int(r0))).x
  const v10: N = tex.dtm.load(ivec2(int(c0), int(r1))).x
  const v11: N = tex.dtm.load(ivec2(int(c1), int(r1))).x
  return mix(mix(v00, v01, tx), mix(v10, v11, tx), ty).add(u.groundZ)
}

/** float bit test (classMask, childDrawnMask): floor(mask / 2^bit) mod 2 */
/**
 * bit test (classMask, childDrawnMask) as an integer shift: 1.0 when bit `bit` (float 0..23) of `mask` is set. The masks
 * are float uniforms or uint texel words below 2^24, exact in float32 (FX2-R2: replaces floor(mask / 2^bit) mod 2, whose
 * pow() cost a transcendental per test on SwiftShader)
 */
const bitOf = (mask: N, bit: N): N => float(uint(mask).shiftRight(uint(bit)).bitAnd(uint(1)))

export interface PointMaterialOptions {
  pointSizeMode: PointSizeMode
  round: boolean
  shading: SceneShadingProvider
}

export function makePointMaterial(create: () => PointsNodeMaterial, tex: PointTextures, u: PointUniforms, o: PointMaterialOptions): PointsNodeMaterial {
  const m = create()
  const vColor = varyingProperty('vec3', 'vPcColor')
  const pos = Fn(() => {
    const f = fetchPoint({ pool: tex.pool, draw: tex.draw, node: tex.node, block: tex.block }, u.numDraws)
    const p: N = f.p
    const w: N = f.w
    // visibility: class mask bit and the node-local fade hash (M05 §6.7.4)
    const cls: N = float(w.z.shiftRight(uint(24))).toVar()
    const keep: N = fract(float(f.local).mul(PC.weyl)).lessThanEqual(f.fade)
    const vis: N = select(keep, bitOf(u.classMask, cls), float(0)).toVar()
    // Lite point size (M05 §6.7.3): one level smaller where the child octant is drawn and faded in
    const q: N = f.q
    const oct: N = select(q.x.greaterThanEqual(0.5), float(4), float(0)).add(select(q.y.greaterThanEqual(0.5), float(2), float(0)))
      .add(select(q.z.greaterThanEqual(0.5), float(1), float(0)))
    const pitch: N = select(bitOf(f.mask, oct).greaterThan(0.5), f.n1.x.mul(0.5), f.n1.x)
    const mv: N = modelViewMatrix.mul(vec4(p, 1))
    const zv: N = max(mv.z.negate(), float(1e-6))
    // ADR-063: non-leaf nodes clamp at maxPx (the tau cap in dense frames), leaf nodes (NodeTable leaf flag) at maxPxLeaf
    const size: N = clamp(u.sizeK.mul(pitch).mul(u.projK).div(zv), u.minPx, select(f.n1.z.greaterThan(0.5), u.maxPxLeaf, u.maxPx))
    if (o.pointSizeMode === 'glpoint') builtin('gl_PointSize').assign(select(vis.greaterThan(0.5), size, float(0)))
    // oct16 normal (AWR-16 §4.5), faced to the eye
    const wn: N = w.y.shiftRight(uint(16)).toVar()
    const ou: N = float(wn.shiftRight(uint(8))).div(255).mul(2).sub(1)
    const ov: N = float(wn.bitAnd(uint(255))).div(255).mul(2).sub(1)
    const nz: N = float(1).sub(abs(ou)).sub(abs(ov))
    const sgnU: N = select(ou.greaterThanEqual(0), float(1), float(-1))
    const sgnV: N = select(ov.greaterThanEqual(0), float(1), float(-1))
    const nx: N = select(nz.lessThan(0), float(1).sub(abs(ov)).mul(sgnU), ou)
    const ny: N = select(nz.lessThan(0), float(1).sub(abs(ou)).mul(sgnV), ov)
    const n: N = normalize(vec3(nx, ny, nz)).toVar()
    // light term in the three world frame (the provider faces the normal to the eye); points without a normal are unlit
    const posW: N = modelWorldMatrix.mul(vec4(p, 1)).xyz
    const nW: N = normalize(modelWorldMatrix.mul(vec4(n, 0)).xyz)
    const lam: N = select(wn.greaterThan(uint(0)), o.shading.lambert(nW, posW), float(1)).toVar()
    // colour modes: uniform branches, only the selected one runs
    const c: N = vec3(0).toVar()
    const mode: N = u.colorMode
    If(mode.lessThan(1.5), () => {
      // Height (0) and HAG (1); HAG falls back to Height until the DTM is loaded
      const useHag: N = mode.greaterThan(0.5).and(u.dtmReady.greaterThan(0.5))
      const z: N = select(useHag, p.z.sub(dtmAt(u, tex, p.x, p.y)), p.z)
      const lo: N = select(useHag, u.hagLo, u.zLo)
      const hi: N = select(useHag, u.hagHi, u.zHi)
      const t: N = pow(clamp(z.sub(lo).div(max(hi.sub(lo), float(1e-3))), float(0), float(1)), u.gamma)
      c.assign(ramp5(u, t).mul(lam))
    }).ElseIf(mode.lessThan(2.5), () => {
      c.assign(u.normalBase.mul(lam)) // Normal
    }).ElseIf(mode.lessThan(3.5), () => {
      // Class: table colour; class 11 demoted to g50 while a higher-priority red entity is on screen (ADR-032)
      const base: N = tex.classes.load(ivec2(int(cls), 0)).xyz
      const hero: N = cls.equal(PC.heroClass).and(u.heroActive.greaterThan(0.5))
      c.assign(select(hero, u.heroDemoted, base).mul(lam))
    }).ElseIf(mode.lessThan(4.5), () => {
      // Source: col.rgb as stored (sRGB bytes, decoded to linear), no lighting
      const srgb: N = vec3(float(w.z.bitAnd(uint(255))), float(w.z.shiftRight(uint(8)).bitAnd(uint(255))), float(w.z.shiftRight(uint(16)).bitAnd(uint(255)))).div(255)
      c.assign(pow(srgb, vec3(2.2)))
    }).Else(() => {
      // Intensity (stub, needs the ext stream): ext byte 0 through the ramp
      c.assign(ramp5(u, float(w.w.bitAnd(uint(255))).div(255)).mul(lam))
    })
    // scene fog per vertex (ADR-064): the provider's fog colour and 1 - T(camera -> p), as scene.fogNode does per fragment
    const fogged: N = mix(c, o.shading.fogColor, o.shading.fogFactor(posW))
    vColor.assign(outputTransform(fogged))
    return select(vis.greaterThan(0.5), p, u.behind)
  })
  m.positionNode = pos()
  m.colorNode = o.round
    ? Fn(() => {
      Discard(length((pointUV as N).sub(0.5)).greaterThan(0.5))
      return vec4(vColor, 1)
    })()
    : vec4(vColor, 1)
  m.transparent = false
  m.depthWrite = true
  m.depthTest = true
  m.fog = false // applied per vertex above (ADR-064)
  m.userData.awrOutputInVertex = true
  return m
}

/**
 * texture nodes for a set of textures; keep them to rebind with .value = newTexture. Every access is a texel load with
 * integer coordinates: three's default uv transform (a mat3 multiply on each load, enabled for texture(t) without uv)
 * is switched off (FX2-R2: about 20 loads per point in the vertex stage).
 */
export function makeTextureNodes(t: { pool: DataTexture; draw: DataTexture; block: DataTexture; node: DataTexture; dtm: DataTexture; classes: DataTexture }): PointTextures {
  const tn = (x: DataTexture): N => (texture(x) as N).setUpdateMatrix(false)
  return { pool: tn(t.pool), draw: tn(t.draw), block: tn(t.block), node: tn(t.node), dtm: tn(t.dtm), classes: tn(t.classes) }
}
