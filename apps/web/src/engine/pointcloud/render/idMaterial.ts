// Pick ID material (D1-ext; M05 §6.9, M05-FR-047; g02 §4.4; r13 §3.6). Owner: M05. Same pulling code and Lite point size
// as the point material, over the pick sub-table (only the DrawTable entries whose tight box meets the pick ray); it
// writes id = vid + 1 as three bytes (RGB of an RGBA8 target, little endian) so M06's 5 x 5 raster-px pick pass reads the point back
// exactly (render targets receive linear values, no output transform on the classic path). Hidden points (class mask,
// fade hash) are dropped the same way as in the point material. One program per tier; no uniform changes its graph.
import type { PointsNodeMaterial } from 'three/webgpu'
import { Fn, builtin, clamp, float, floor, fract, max, mod, modelViewMatrix, select, uint, varyingProperty, vec4 } from 'three/tsl'
import type { PointSizeMode } from '../../loop'
import { PC } from '../params'
import { fetchPoint, type N } from './fetchNode'
import type { PointTextures, PointUniforms } from './pointMaterial'

/**
 * bit test (classMask, childDrawnMask) as an integer shift: 1.0 when bit `bit` (float 0..23) of `mask` is set. The masks
 * are float uniforms or uint texel words below 2^24, exact in float32 (FX2-R2: replaces floor(mask / 2^bit) mod 2, whose
 * pow() cost a transcendental per test on SwiftShader)
 */
const bitOf = (mask: N, bit: N): N => float(uint(mask).shiftRight(uint(bit)).bitAnd(uint(1)))

export function makeIdMaterial(create: () => PointsNodeMaterial, tex: PointTextures & { pick: N }, u: PointUniforms, numDraws: N, o: { pointSizeMode: PointSizeMode }): PointsNodeMaterial {
  const m = create()
  const vId = varyingProperty('float', 'vPcPickId')
  m.positionNode = Fn(() => {
    const f = fetchPoint({ pool: tex.pool, draw: tex.pick, node: tex.node }, numDraws)
    const w: N = f.w
    const cls: N = float(w.z.shiftRight(uint(24)))
    const keep: N = fract(float(f.local).mul(PC.weyl)).lessThanEqual(f.fade)
    const vis: N = select(keep, bitOf(u.classMask, cls), float(0)).toVar()
    const q: N = f.q
    const oct: N = select(q.x.greaterThanEqual(0.5), float(4), float(0)).add(select(q.y.greaterThanEqual(0.5), float(2), float(0)))
      .add(select(q.z.greaterThanEqual(0.5), float(1), float(0)))
    const pitch: N = select(bitOf(f.mask, oct).greaterThan(0.5), f.n1.x.mul(0.5), f.n1.x)
    const mv: N = modelViewMatrix.mul(vec4(f.p, 1))
    const size: N = clamp(u.sizeK.mul(pitch).mul(u.projK).div(max(mv.z.negate(), float(1e-6))), u.minPx, select(f.n1.z.greaterThan(0.5), u.maxPxLeaf, u.maxPx))
    if (o.pointSizeMode === 'glpoint') builtin('gl_PointSize').assign(select(vis.greaterThan(0.5), size, float(0)))
    vId.assign(float(f.vid.add(1)))
    return select(vis.greaterThan(0.5), f.p, u.behind)
  })()
  const id: N = floor(vId.add(0.5))
  // 24-bit id in RGB (opaque node materials force alpha to 1); a pick table never exceeds B <= 12M < 2^24 points
  m.colorNode = vec4(mod(id, 256).div(255), mod(floor(id.div(256)), 256).div(255), mod(floor(id.div(65536)), 256).div(255), 1)
  m.transparent = false
  m.depthWrite = true
  m.depthTest = true
  m.fog = false
  return m
}
