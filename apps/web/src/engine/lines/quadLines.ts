// Screen-space wide line segments without instancing (FX2-R3, ADR-067; M06 §6.10 "line width and material"). Owner: M06.
// Every segment is an explicit quad: 4 vertices (6 indices) that all carry the segment's start and end (xyz), a vec2 of
// per-segment extras (trail times, dash distances) and a palette index in one interleaved buffer (stride 9 floats), plus a
// static corner attribute (along 0 | 1, side -1 | +1). The vertex stage projects both ends (view-space trim onto the
// near plane when one end is behind the camera), offsets the corner by half the raster width across the segment and by
// half the width along it (square caps, so consecutive segments join), and collapses invalid segments and segments
// entirely behind the camera to one point outside the clip volume (no raster). Widths are in raster px (uniform), the
// viewport is the drawing-buffer size; both are set once per frame by the owner.
// Why: SwiftShader (Tier S) runs an instanced draw instance by instance, about 25 us of frame time per instance on this
// machine; LineSegments2 + Line2NodeMaterial (one instance per segment) made a 2 x 1024-segment selected trail cost
// 50 ms of frame time and a few hundred planned-path segments several ms. Non-instanced quads are one ordinary draw.
import { BufferAttribute, BufferGeometry, DynamicDrawUsage, InterleavedBuffer, InterleavedBufferAttribute, Sphere, Vector2, Vector3 } from 'three'
import { Fn, If, attribute, cameraProjectionMatrix, length, mix, modelViewMatrix, select, uniform, varying, vec2, vec4 } from 'three/tsl'

type N = any // TSL nodes

/** floats per vertex: start xyz, end xyz, extra (2), palette index */
export const QL_STRIDE = 9
export const QL_VPS = 4

export interface QuadLineUniforms {
  /** raster width in px */
  widthPx: N
  /** drawing-buffer size in px (Vector2) */
  viewport: N
  /** camera near plane (m) */
  near: N
}

export function makeQuadLineUniforms(): QuadLineUniforms {
  return { widthPx: uniform(1), viewport: uniform(new Vector2(1, 1)), near: uniform(0.1) }
}

/** set the per-frame uniforms: CSS width and raster/CSS ratio (raster width >= 1 px), drawing buffer, camera near */
export function setQuadLineView(u: QuadLineUniforms, widthCss: number, dpr: number, dbW: number, dbH: number, near: number): void {
  u.widthPx.value = Math.max(widthCss * Math.max(dpr, 1e-3), 1)
  const v = u.viewport.value as Vector2
  if (dbW > 0 && dbH > 0) v.set(dbW, dbH)
  if (near > 0) u.near.value = near
}

export interface QuadLineGeometry { geometry: BufferGeometry; data: Float32Array; buf: InterleavedBuffer }

/** geometry for capSegments quads: interleaved dynamic data, static corners and indices; draw range 0 */
export function makeQuadLineGeometry(capSegments: number): QuadLineGeometry {
  const data = new Float32Array(capSegments * QL_VPS * QL_STRIDE)
  const buf = new InterleavedBuffer(data, QL_STRIDE)
  buf.setUsage(DynamicDrawUsage)
  const corner = new Float32Array(capSegments * QL_VPS * 2)
  const index = capSegments * QL_VPS > 65535 ? new Uint32Array(capSegments * 6) : new Uint16Array(capSegments * 6)
  for (let i = 0; i < capSegments; i++) {
    const v = i * QL_VPS
    corner.set([0, -1, 0, 1, 1, -1, 1, 1], 2 * v)
    index.set([v, v + 1, v + 2, v + 2, v + 1, v + 3], 6 * i)
  }
  const g = new BufferGeometry()
  g.setAttribute('qlStart', new InterleavedBufferAttribute(buf, 3, 0))
  g.setAttribute('qlEnd', new InterleavedBufferAttribute(buf, 3, 3))
  g.setAttribute('qlExtra', new InterleavedBufferAttribute(buf, 2, 6))
  g.setAttribute('qlColor', new InterleavedBufferAttribute(buf, 1, 8))
  g.setAttribute('qlCorner', new BufferAttribute(corner, 2))
  g.setIndex(new BufferAttribute(index, 1))
  g.setDrawRange(0, 0)
  g.boundingSphere = new Sphere(new Vector3(), 1e7)
  return { geometry: g, data, buf }
}

/** write segment i (all 4 vertices) */
export function writeQuadLine(d: Float32Array, i: number, ax: number, ay: number, az: number, bx: number, by: number, bz: number, e0: number, e1: number, color: number): void {
  for (let v = 0; v < QL_VPS; v++) {
    const o = (i * QL_VPS + v) * QL_STRIDE
    d[o] = ax
    d[o + 1] = ay
    d[o + 2] = az
    d[o + 3] = bx
    d[o + 4] = by
    d[o + 5] = bz
    d[o + 6] = e0
    d[o + 7] = e1
    d[o + 8] = color
  }
}

/** write only the extras of segment i (all 4 vertices) */
export function writeQuadLineExtra(d: Float32Array, i: number, e0: number, e1: number): void {
  for (let v = 0; v < QL_VPS; v++) {
    const o = (i * QL_VPS + v) * QL_STRIDE
    d[o + 6] = e0
    d[o + 7] = e1
  }
}

/** write only the palette index of segments [from, to) */
export function writeQuadLineColor(d: Float32Array, from: number, to: number, color: number): void {
  for (let i = from * QL_VPS; i < to * QL_VPS; i++) d[i * QL_STRIDE + 8] = color
}

/** TSL nodes of the quad-line attributes: corner (vec2), extra (vec2), the extra at this vertex's end (varying) and the palette index (varying) */
export function quadLineAttributes(): { corner: N; extra: N; extraAt: N; colorIdx: N } {
  const corner: N = attribute('qlCorner', 'vec2')
  const extra: N = attribute('qlExtra', 'vec2')
  return { corner, extra, extraAt: varying(mix(extra.x, extra.y, corner.x)), colorIdx: varying(attribute('qlColor', 'float')) }
}

/** vertex node (clip position) of the quad lines; valid(extra) false collapses the segment */
export function quadLineVertexNode(u: QuadLineUniforms, valid: (extra: N) => N): N {
  const corner: N = attribute('qlCorner', 'vec2')
  const extra: N = attribute('qlExtra', 'vec2')
  const vp: N = u.viewport
  const zNear: N = u.near.mul(-1.0001)
  return Fn(() => {
    const out: N = vec4(2, 2, 2, 1).toVar()
    const a: N = modelViewMatrix.mul(vec4(attribute('qlStart', 'vec3'), 1)).xyz.toVar()
    const b: N = modelViewMatrix.mul(vec4(attribute('qlEnd', 'vec3'), 1)).xyz.toVar()
    const aIn: N = a.z.lessThanEqual(zNear).toVar()
    const bIn: N = b.z.lessThanEqual(zNear).toVar()
    If(valid(extra).and(aIn.or(bIn)), () => {
      // trim the end behind the near plane onto it (view space, the camera looks down -z)
      If(aIn.not(), () => {
        a.assign(mix(a, b, zNear.sub(a.z).div(b.z.sub(a.z))))
      })
      If(bIn.not(), () => {
        b.assign(mix(b, a, zNear.sub(b.z).div(a.z.sub(b.z))))
      })
      const ca: N = cameraProjectionMatrix.mul(vec4(a, 1)).toVar()
      const cb: N = cameraProjectionMatrix.mul(vec4(b, 1)).toVar()
      const d: N = cb.xy.div(cb.w).sub(ca.xy.div(ca.w)).mul(vp).toVar()
      const len: N = length(d)
      const dir: N = select(len.greaterThan(1e-6), d.div(len), vec2(1, 0)).toVar()
      const nrm: N = vec2(dir.y.negate(), dir.x)
      // offsets in raster px: half the width across, half along (square cap) outward from this end
      const offPx: N = nrm.mul(corner.y).add(dir.mul(corner.x.mul(2).sub(1))).mul(u.widthPx.mul(0.5))
      const c: N = select(corner.x.greaterThan(0.5), cb, ca).toVar()
      out.assign(vec4(c.xy.add(offPx.div(vp).mul(2).mul(c.w)), c.z, c.w))
    })
    return out
  })()
}

/** queue n dirty segment ranges [lo[k], hi[k]] for upload; a full upload when lo is null (no allocation) */
export function flushQuadLines(buf: InterleavedBuffer, lo: Int32Array | null, hi: Int32Array | null = null, n = 0): void {
  buf.clearUpdateRanges()
  if (lo && hi) for (let k = 0; k < n; k++) buf.addUpdateRange(lo[k] * QL_VPS * QL_STRIDE, (hi[k] - lo[k] + 1) * QL_VPS * QL_STRIDE)
  buf.needsUpdate = true
}
