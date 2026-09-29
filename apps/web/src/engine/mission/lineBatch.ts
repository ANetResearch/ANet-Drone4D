// Line batches for overlays (M06 §6.10 "line width and material"; AWR-15 §10.6, §10.12). Owner: M06.
// ThinLineBatch: 1 px lines as one LineSegments + LineBasicNodeMaterial (g01 §3: native 1 px lines stay 1 px at the
// Tier S raster). Per vertex: palette index, dashed flag, line distance (m) and alpha; dashes are discarded in the
// fragment with a uniform period (same program for solid and dashed). Used for executed paths, area outlines, zone
// vertical edges and the GoTo plumb line. WideLineBatch: >= 1.5 px lines as LineSegments2 + Line2NodeMaterial (solid or
// dashed; dashes in world metres), used for planned paths and zone top outlines. Both rebuild on data change only
// (event driven, <= 4 Hz) and own their geometry (M06 §6.3 rule 11).
import { BufferAttribute, BufferGeometry, InstancedInterleavedBuffer, InterleavedBufferAttribute, LineSegments, NormalBlending, Sphere, Vector3 } from 'three'
import { Line2NodeMaterial, LineBasicNodeMaterial } from 'three/webgpu'
import { LineSegments2 } from 'three/addons/lines/webgpu/LineSegments2.js'
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js'
import { Fn, attribute, mod, select, uniform, varying, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'

type N = any // TSL nodes

/** palette of overlay lines: glyphPalette order (0 g50, 1 r500, 2 g500, 3 g400, 4 g300, 5 g200) */
function paletteSelect(idx: N): N {
  const cols = SCENE.glyphPalette.map((c) => uniform(new Vector3(c[0], c[1], c[2])))
  let c: N = cols[cols.length - 1]
  for (let i = cols.length - 2; i >= 0; i--) c = select(idx.lessThan(i + 0.5), cols[i], c)
  return c
}

export class ThinLineBatch {
  readonly obj: LineSegments
  private readonly pos: BufferAttribute
  private readonly info: BufferAttribute // palette, dashed, distance, alpha
  n = 0
  private readonly uDash: N
  private readonly uGap: N

  constructor(readonly capSegments: number, name: string, renderOrder: number, depthTest = true) {
    const g = new BufferGeometry()
    this.pos = new BufferAttribute(new Float32Array(capSegments * 6), 3)
    this.info = new BufferAttribute(new Float32Array(capSegments * 8), 4)
    this.pos.setUsage(35048)
    this.info.setUsage(35048)
    g.setAttribute('position', this.pos)
    g.setAttribute('lineInfo', this.info)
    g.setDrawRange(0, 0)
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    const m = new LineBasicNodeMaterial()
    this.uDash = uniform(3)
    this.uGap = uniform(6)
    const info: N = varying(attribute('lineInfo', 'vec4'))
    m.colorNode = vec4(paletteSelect(info.x), 1) as N
    m.opacityNode = Fn(() => {
      info.y.greaterThan(0.5).and(mod(info.z, this.uDash.add(this.uGap)).greaterThan(this.uDash)).discard()
      return info.w
    })() as N
    m.transparent = true
    m.depthWrite = false
    m.depthTest = depthTest
    m.fog = false
    this.obj = new LineSegments(g, m)
    this.obj.frustumCulled = false
    this.obj.renderOrder = renderOrder
    this.obj.name = name
    this.obj.visible = false
  }

  begin(): void {
    this.n = 0
  }

  /** one segment (ENU m); dist0 = line distance at a (m) for dash continuity; returns false when full */
  seg(ax: number, ay: number, az: number, bx: number, by: number, bz: number, palette: number, dashed: boolean, alpha: number, dist0 = 0): boolean {
    if (this.n >= this.capSegments) return false
    const P = this.pos.array as Float32Array
    const I = this.info.array as Float32Array
    const o = this.n * 6
    P[o] = ax
    P[o + 1] = ay
    P[o + 2] = az
    P[o + 3] = bx
    P[o + 4] = by
    P[o + 5] = bz
    const k = this.n * 8
    const d1 = dist0 + Math.hypot(bx - ax, by - ay, bz - az)
    I[k] = palette
    I[k + 1] = dashed ? 1 : 0
    I[k + 2] = dist0
    I[k + 3] = alpha
    I[k + 4] = palette
    I[k + 5] = dashed ? 1 : 0
    I[k + 6] = d1
    I[k + 7] = alpha
    this.n++
    return true
  }

  /** upload segments [fromSeg, n) (only what changed) and set the draw range */
  commit(fromSeg = 0): void {
    this.obj.geometry.setDrawRange(0, 2 * this.n)
    this.obj.visible = this.n > 0
    if (this.n > fromSeg) {
      this.pos.clearUpdateRanges()
      this.pos.addUpdateRange(fromSeg * 6, (this.n - fromSeg) * 6)
      this.info.clearUpdateRanges()
      this.info.addUpdateRange(fromSeg * 8, (this.n - fromSeg) * 8)
      this.pos.needsUpdate = true
      this.info.needsUpdate = true
    }
  }

  drawCount(): number {
    return this.obj.visible ? 1 : 0
  }

  dispose(): void {
    this.obj.geometry.dispose()
    ;(this.obj.material as LineBasicNodeMaterial).dispose()
  }
}

/** Line2NodeMaterial whose transparent path blends normally (no WebGPU viewport mip texture under the handler) */
class OverlayLine2Material extends Line2NodeMaterial {
  setupDiffuseColor(builder: unknown): void {
    const t = this.transparent
    this.transparent = false
    ;(Line2NodeMaterial.prototype as unknown as { setupDiffuseColor(b: unknown): void }).setupDiffuseColor.call(this, builder)
    this.transparent = t
  }
}

export class WideLineBatch {
  readonly obj: LineSegments2
  private readonly pos: Float32Array
  private readonly dist: Float32Array
  private readonly col: Float32Array
  private readonly posBuf: InstancedInterleavedBuffer
  private readonly distBuf: InstancedInterleavedBuffer
  private readonly colBuf: InstancedInterleavedBuffer
  private readonly material: OverlayLine2Material
  private readonly uAlpha: N
  n = 0

  constructor(readonly capSegments: number, readonly widthCss: number, dashed: boolean, name: string, renderOrder: number, alpha = 1) {
    this.pos = new Float32Array(capSegments * 6)
    this.dist = new Float32Array(capSegments * 2)
    this.col = new Float32Array(capSegments)
    this.posBuf = new InstancedInterleavedBuffer(this.pos, 6, 1)
    this.distBuf = new InstancedInterleavedBuffer(this.dist, 2, 1)
    this.colBuf = new InstancedInterleavedBuffer(this.col, 1, 1)
    const g = new LineSegmentsGeometry()
    g.setAttribute('instanceStart', new InterleavedBufferAttribute(this.posBuf, 3, 0))
    g.setAttribute('instanceEnd', new InterleavedBufferAttribute(this.posBuf, 3, 3))
    g.setAttribute('instanceDistanceStart', new InterleavedBufferAttribute(this.distBuf, 1, 0))
    g.setAttribute('instanceDistanceEnd', new InterleavedBufferAttribute(this.distBuf, 1, 1))
    g.setAttribute('instanceColorIdx', new InterleavedBufferAttribute(this.colBuf, 1, 0))
    g.instanceCount = 0
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    const m = new OverlayLine2Material({ linewidth: widthCss, worldUnits: false, dashed, dashSize: 3, gapSize: 6 })
    this.uAlpha = uniform(alpha)
    const idx: N = varying(attribute('instanceColorIdx', 'float'))
    m.colorNode = vec4(paletteSelect(idx), 1) as N
    m.opacityNode = this.uAlpha
    m.transparent = true
    m.blending = NormalBlending
    m.depthWrite = false
    m.fog = false
    this.material = m
    this.obj = new LineSegments2(g, m)
    this.obj.frustumCulled = false
    this.obj.renderOrder = renderOrder
    this.obj.name = name
    this.obj.visible = false
  }

  begin(): void {
    this.n = 0
  }

  seg(ax: number, ay: number, az: number, bx: number, by: number, bz: number, palette: number, dist0 = 0): boolean {
    if (this.n >= this.capSegments) return false
    const o = this.n * 6
    const P = this.pos
    P[o] = ax
    P[o + 1] = ay
    P[o + 2] = az
    P[o + 3] = bx
    P[o + 4] = by
    P[o + 5] = bz
    this.dist[2 * this.n] = dist0
    this.dist[2 * this.n + 1] = dist0 + Math.hypot(bx - ax, by - ay, bz - az)
    this.col[this.n] = palette
    this.n++
    return true
  }

  commit(): void {
    const g = this.obj.geometry as LineSegmentsGeometry
    g.instanceCount = this.n
    this.obj.visible = this.n > 0
    this.posBuf.needsUpdate = true
    this.distBuf.needsUpdate = true
    this.colBuf.needsUpdate = true
  }

  setAlpha(a: number): void {
    this.uAlpha.value = a
  }

  /** change the palette of segments [from, from + count) (attribute update only; no rebuild, no recompile) */
  recolor(from: number, count: number, palette: number): void {
    for (let i = from; i < Math.min(this.n, from + count); i++) this.col[i] = palette
    this.colBuf.needsUpdate = true
  }

  setWidth(dpr: number): void {
    this.material.linewidth = Math.max(this.widthCss, 1 / Math.max(dpr, 1e-3))
  }

  drawCount(): number {
    return this.obj.visible ? 1 : 0
  }

  dispose(): void {
    this.obj.geometry.dispose()
    this.material.dispose()
  }
}
