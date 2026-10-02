// Line batches for overlays (M06 §6.10 "line width and material"; AWR-15 §10.6, §10.12). Owner: M06.
// ThinLineBatch: 1 px lines as one LineSegments + LineBasicNodeMaterial (g01 §3: native 1 px lines stay 1 px at the
// Tier S raster). Per vertex: palette index, dashed flag, line distance (m) and alpha; dashes are discarded in the
// fragment with a uniform period (same program for solid and dashed). Used for executed paths, area outlines, zone
// vertical edges and the GoTo plumb line. WideLineBatch: >= 1.5 px lines as non-instanced screen-space quads
// (engine/lines/quadLines.ts; solid or dashed, dashes in world metres; FX2-R3, ADR-067: the instanced LineSegments2 +
// Line2NodeMaterial costs per segment on SwiftShader), used for planned paths and zone top outlines. Both rebuild on data change only
// (event driven, <= 4 Hz) and own their geometry (M06 §6.3 rule 11).
import { BufferAttribute, BufferGeometry, DoubleSide, LineSegments, Mesh, NormalBlending, Sphere, Vector3, type InterleavedBuffer } from 'three'
import { LineBasicNodeMaterial, MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, attribute, bool, float, mod, select, uniform, varying, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import {
  flushQuadLines, makeQuadLineGeometry, makeQuadLineUniforms, quadLineAttributes, quadLineVertexNode, setQuadLineView, writeQuadLine,
  writeQuadLineColor, type QuadLineUniforms,
} from '../lines/quadLines'

/** dashes of the wide dashed lines, world metres */
export const WIDE_DASH = { on: 3, off: 6 } as const

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

export class WideLineBatch {
  readonly obj: Mesh
  private readonly data: Float32Array
  private readonly buf: InterleavedBuffer
  private readonly geometry: BufferGeometry
  private readonly material: MeshBasicNodeMaterial
  private readonly uAlpha: N
  private readonly u: QuadLineUniforms
  private dpr = 1
  n = 0

  constructor(readonly capSegments: number, readonly widthCss: number, dashed: boolean, name: string, renderOrder: number, alpha = 1) {
    const q = makeQuadLineGeometry(capSegments)
    this.data = q.data
    this.buf = q.buf
    this.geometry = q.geometry
    this.u = makeQuadLineUniforms()
    const m = new MeshBasicNodeMaterial({ side: DoubleSide })
    const a = quadLineAttributes()
    m.vertexNode = quadLineVertexNode(this.u, () => bool(true)) as N
    this.uAlpha = uniform(alpha)
    m.colorNode = paletteSelect(a.colorIdx)
    // dashes in world metres along the line (3 m on, 6 m off), as the Line2NodeMaterial batch had
    m.opacityNode = dashed ? Fn(() => {
      mod(a.extraAt, float(WIDE_DASH.on + WIDE_DASH.off)).greaterThan(WIDE_DASH.on).discard()
      return this.uAlpha
    })() as N : this.uAlpha
    m.transparent = true
    m.blending = NormalBlending
    m.depthWrite = false
    m.fog = false
    // three draws transparent double-sided materials twice unless single pass (pass plan, INT-1)
    m.forceSinglePass = true
    this.material = m
    this.obj = new Mesh(this.geometry, m)
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
    writeQuadLine(this.data, this.n, ax, ay, az, bx, by, bz, dist0, dist0 + Math.hypot(bx - ax, by - ay, bz - az), palette)
    this.n++
    return true
  }

  commit(): void {
    this.geometry.setDrawRange(0, this.n * 6)
    this.obj.visible = this.n > 0
    flushQuadLines(this.buf, null)
  }

  setAlpha(a: number): void {
    this.uAlpha.value = a
  }

  /** change the palette of segments [from, from + count) (attribute update only; no rebuild, no recompile) */
  recolor(from: number, count: number, palette: number): void {
    writeQuadLineColor(this.data, from, Math.min(this.n, from + count), palette)
    flushQuadLines(this.buf, null)
  }

  /** raster/CSS ratio (the raster width is at least 1 px); the drawing buffer and the camera near plane once per frame */
  setWidth(dpr: number): void {
    this.dpr = dpr
    this.u.widthPx.value = Math.max(this.widthCss * Math.max(dpr, 1e-3), 1)
  }

  setView(dbW: number, dbH: number, near: number): void {
    setQuadLineView(this.u, this.widthCss, this.dpr, dbW, dbH, near)
  }

  /** shader zoo warm-up: draw one segment (whatever it holds); commit() restores the range */
  warmBefore(): void {
    this.geometry.setDrawRange(0, 6)
  }

  drawCount(): number {
    return this.obj.visible ? 1 : 0
  }

  dispose(): void {
    this.geometry.dispose()
    this.material.dispose()
  }
}
