// Attribute-less screen-space quads (M06 §6.9 markers, §6.10 GlyphLayer; g01 §3 T6; r16 §0 item 2). Owner: M06.
// One draw of 6 vertices per instance (drawRange = 6 n, no position attribute: three limits the draw by the draw range
// only, g01 §2). vertexIndex / 6 selects the instance, vertexIndex mod 6 the corner of two CCW triangles. The instance
// centre is projected with the object's model-view (layer frame ENU under WorldRoot) and the corner is offset in clip
// space by (size in raster px / viewport raster px) x w, so sizes are exact raster pixels at any DPR (AWR-15 §10.1 item 4:
// w_rt = max(1, w_css x dpr)). Instance data comes from an RGBA32F DataTexture (textureLoad, no filtering).
import { DataTexture, FloatType, NearestFilter, RGBAFormat } from 'three'
import { cameraProjectionMatrix, float, floor, int, ivec2, modelViewMatrix, step, textureLoad, vec2, vec4, vertexIndex } from 'three/tsl'

type N = any // TSL nodes

/** RGBA32F instance texture of `width` x `height` texels, CPU array written in place (needsUpdate per frame) */
export function makeInstanceTexture(width: number, height = 1, name = 'instances'): DataTexture {
  const t = new DataTexture(new Float32Array(width * height * 4), width, height, RGBAFormat, FloatType)
  t.minFilter = NearestFilter
  t.magFilter = NearestFilter
  t.generateMipmaps = false
  t.name = name
  t.needsUpdate = true
  return t
}

/** instance index and corner in [-1, 1]^2 of the current vertex */
export function quadVertex(): { iid: N; corner: N } {
  const vf: N = float(vertexIndex)
  const iid: N = floor(vf.div(6))
  const c: N = vf.sub(iid.mul(6))
  const cx: N = step(0.5, c).mul(float(1).sub(step(2.5, c))).add(step(3.5, c).mul(float(1).sub(step(4.5, c))))
  const cy: N = step(1.5, c).mul(float(1).sub(step(2.5, c))).add(step(3.5, c))
  return { iid, corner: vec2(cx, cy).mul(2).sub(1) }
}

/** texel k of instance iid in a texture whose width is 2^log2w (texelsPerInstance texels per instance) */
export function instanceTexel(tex: DataTexture, iid: N, k: number, texelsPerInstance: number, log2w: number): N {
  const i: N = int(iid).mul(texelsPerInstance).add(k).toVar()
  return textureLoad(tex, ivec2(i.bitAnd((1 << log2w) - 1), i.shiftRight(log2w)))
}

/** clip position of a layer-frame point offset by corner x half size (raster px) */
export function clipQuad(p: N, corner: N, halfPx: N, viewportPx: N): N {
  const clip: N = cameraProjectionMatrix.mul(modelViewMatrix).mul(vec4(p, 1)).toVar()
  const off: N = corner.mul(halfPx).mul(2).div(viewportPx).mul(clip.w)
  return vec4(clip.xy.add(off), clip.z, clip.w)
}

/** a clip position outside the view volume (hidden instances collapse there); a new node per material */
export const hiddenClip = (): N => vec4(2, 2, 2, 1)
