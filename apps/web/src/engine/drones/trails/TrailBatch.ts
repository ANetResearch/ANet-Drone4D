// GPU trail batch (M06-FR-042, AC-033; M06 §6.10; AWR-15 §10.5; g01 §3 T11e). Owner: M06.
// One LineSegments2 (three/addons/lines/webgpu) with Line2NodeMaterial per batch (focus set 1 px g400, selected 2 px
// g50 or r500, selected halo 4 px --drone-halo 60 %); every batch owns its geometry (M06 §6.3 rule 11). Instance data:
// instanceStart/End (xyz, interleaved), instanceTime (start, end, sim seconds relative to the trail block), and
// instanceColor (palette index). Segment slots: slot s owns [s x segs, (s + 1) x segs); empty segments are degenerate
// with time -1 and are discarded. The age alpha a = mix(0.8, 0.15, clamp((uNow - t) / 120 s, 0, 1)) is evaluated in
// the shader from uNow = tRender - blockStart, so the geometry is never rebuilt per frame; beyond the window the
// fragment is discarded. Line widths in CSS px are clamped so the raster width is at least 1 px (w_rt >= 1).
import { InstancedInterleavedBuffer, InterleavedBufferAttribute, NormalBlending, Sphere, Vector3 } from 'three'
import { Line2NodeMaterial } from 'three/webgpu'
import { LineSegments2 } from 'three/addons/lines/webgpu/LineSegments2.js'
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js'
import { Fn, attribute, clamp, float, mix, positionGeometry, select, uniform, varying, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { TRAIL, type TrailRing } from './TrailRing'

type N = any // TSL nodes

/** Line2NodeMaterial whose transparent path blends normally instead of reading the WebGPU viewport mip texture */
class TrailLineMaterial extends Line2NodeMaterial {
  setupDiffuseColor(builder: unknown): void {
    const t = this.transparent
    this.transparent = false
    ;(Line2NodeMaterial.prototype as unknown as { setupDiffuseColor(b: unknown): void }).setupDiffuseColor.call(this, builder)
    this.transparent = t
  }
}

export interface TrailStyle { widthCss: number; colors: readonly (readonly [number, number, number])[]; alpha: number; renderOrder: number; name: string }

export class TrailBatch {
  readonly mesh: LineSegments2
  private readonly pos: Float32Array
  private readonly time: Float32Array
  private readonly color: Float32Array
  private readonly posBuf: InstancedInterleavedBuffer
  private readonly timeBuf: InstancedInterleavedBuffer
  private readonly colorAttr: InstancedInterleavedBuffer
  private readonly material: TrailLineMaterial
  private readonly uNow: N
  /** agent per slot (-1 empty), last synced ring sequence, write cursor */
  readonly slotAgent: Int32Array
  private readonly slotSeq: Float64Array
  private readonly slotCursor: Int32Array
  private readonly slotColor: Float32Array
  private readonly tmp = new Float32Array(8)
  segs: number
  activeSlots: number

  constructor(readonly maxSlots: number, readonly maxSegs: number, readonly style: TrailStyle) {
    this.segs = maxSegs
    this.activeSlots = maxSlots
    const n = maxSlots * maxSegs
    this.pos = new Float32Array(n * 6)
    this.time = new Float32Array(n * 2).fill(-1)
    this.color = new Float32Array(n)
    this.posBuf = new InstancedInterleavedBuffer(this.pos, 6, 1)
    this.timeBuf = new InstancedInterleavedBuffer(this.time, 2, 1)
    this.colorAttr = new InstancedInterleavedBuffer(this.color, 1, 1)
    const g = new LineSegmentsGeometry()
    g.setAttribute('instanceStart', new InterleavedBufferAttribute(this.posBuf, 3, 0))
    g.setAttribute('instanceEnd', new InterleavedBufferAttribute(this.posBuf, 3, 3))
    g.setAttribute('instanceTimeStart', new InterleavedBufferAttribute(this.timeBuf, 1, 0))
    g.setAttribute('instanceTimeEnd', new InterleavedBufferAttribute(this.timeBuf, 1, 1))
    g.setAttribute('instanceColorIdx', new InterleavedBufferAttribute(this.colorAttr, 1, 0))
    g.instanceCount = n
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    this.uNow = uniform(0)
    const m = new TrailLineMaterial({ linewidth: style.widthCss, worldUnits: false, dashed: false })
    const now = this.uNow
    const cols = style.colors.map((c) => uniform(new Vector3(c[0], c[1], c[2])))
    // vertex-stage time along the segment (positionGeometry.y is 0 at the start and 1 at the end of the quad)
    const tAt: N = varying(mix(attribute('instanceTimeStart', 'float'), attribute('instanceTimeEnd', 'float'), clamp((positionGeometry as N).y, float(0), float(1))))
    const idx: N = varying(attribute('instanceColorIdx', 'float'))
    const age: N = now.sub(tAt)
    m.opacityNode = Fn(() => {
      tAt.lessThan(0).or(age.greaterThan(TRAIL.windowS)).discard()
      return mix(float(0.8), float(0.15), clamp(age.div(TRAIL.windowS), float(0), float(1))).mul(style.alpha)
    })() as N
    let c: N = cols[cols.length - 1]
    for (let i = cols.length - 2; i >= 0; i--) c = select(idx.lessThan(i + 0.5), cols[i], c)
    m.colorNode = vec4(c, 1) as N
    m.transparent = true
    m.blending = NormalBlending
    m.depthWrite = false
    m.depthTest = true
    m.fog = false
    this.material = m
    this.mesh = new LineSegments2(g, m)
    this.mesh.frustumCulled = false
    this.mesh.renderOrder = style.renderOrder
    this.mesh.name = style.name
    this.mesh.visible = false
    this.slotAgent = new Int32Array(maxSlots).fill(-1)
    this.slotSeq = new Float64Array(maxSlots)
    this.slotCursor = new Int32Array(maxSlots)
    this.slotColor = new Float32Array(maxSlots)
  }

  /** GovernorKnob 1: number of slots in use and segments per slot (<= the allocated maxima); re-layout on change */
  setLimits(slots: number, segs: number, ring: TrailRing): void {
    const s = Math.max(0, Math.min(this.maxSlots, slots))
    const k = Math.max(1, Math.min(this.maxSegs, segs))
    if (s === this.activeSlots && k === this.segs) return
    const agents = Array.from(this.slotAgent).filter((a) => a >= 0).slice(0, s)
    const colors = Array.from(this.slotColor)
    this.activeSlots = s
    this.segs = k
    this.time.fill(-1)
    this.slotAgent.fill(-1)
    for (let i = 0; i < agents.length; i++) this.assign(i, agents[i], colors[i] ?? 0, ring)
    this.posBuf.needsUpdate = true
    this.timeBuf.needsUpdate = true
    this.colorAttr.needsUpdate = true
  }

  /** set the slot's vehicle and copy its CPU history (<= segs segments) */
  assign(slot: number, agentNo: number, colorIdx: number, ring: TrailRing): void {
    this.slotAgent[slot] = agentNo
    this.slotColor[slot] = colorIdx
    const base = slot * this.maxSegs
    for (let j = 0; j < this.segs; j++) {
      this.time[2 * (base + j)] = -1
      this.time[2 * (base + j) + 1] = -1
    }
    this.slotCursor[slot] = 0
    const r = ring.rowFor(agentNo)
    this.slotSeq[slot] = r >= 0 ? ring.seq[r] : 0
    if (r < 0) return
    const c = ring.count[r]
    const first = Math.max(1, c - this.segs)
    for (let j = first; j < c; j++) {
      ring.sample(r, j - 1, this.tmp, 0)
      ring.sample(r, j, this.tmp, 4)
      this.writeSeg(slot, this.tmp, colorIdx)
    }
    this.posBuf.needsUpdate = true
    this.timeBuf.needsUpdate = true
    this.colorAttr.needsUpdate = true
  }

  release(slot: number): void {
    const base = slot * this.maxSegs
    for (let j = 0; j < this.maxSegs; j++) this.time[2 * (base + j)] = this.time[2 * (base + j) + 1] = -1
    this.slotAgent[slot] = -1
    this.timeBuf.needsUpdate = true
  }

  private writeSeg(slot: number, s: Float32Array, colorIdx: number): void {
    const i = slot * this.maxSegs + this.slotCursor[slot]
    this.slotCursor[slot] = (this.slotCursor[slot] + 1) % this.segs
    const p = this.pos
    p[6 * i] = s[0]
    p[6 * i + 1] = s[1]
    p[6 * i + 2] = s[2]
    p[6 * i + 3] = s[4]
    p[6 * i + 4] = s[5]
    p[6 * i + 5] = s[6]
    this.time[2 * i] = s[3]
    this.time[2 * i + 1] = s[7]
    this.color[i] = colorIdx
  }

  /** per frame: append the new CPU samples of the slotted vehicles; recolour on selection or red-owner changes */
  sync(ring: TrailRing, nowRelS: number, colorOf: (agentNo: number) => number): void {
    let dirty = false
    let any = false
    for (let s = 0; s < this.activeSlots; s++) {
      const a = this.slotAgent[s]
      if (a < 0) continue
      any = true
      const col = colorOf(a)
      if (col !== this.slotColor[s]) {
        this.slotColor[s] = col
        const base = s * this.maxSegs
        this.color.fill(col, base, base + this.maxSegs)
        dirty = true
      }
      const r = ring.rowFor(a)
      if (r < 0) continue
      const seq = ring.seq[r]
      const add = Math.min(seq - this.slotSeq[s], ring.count[r] - 1, this.segs)
      for (let k = add; k >= 1; k--) {
        const c = ring.count[r]
        ring.sample(r, c - k - 1, this.tmp, 0)
        ring.sample(r, c - k, this.tmp, 4)
        this.writeSeg(s, this.tmp, col)
        dirty = true
      }
      this.slotSeq[s] = seq
    }
    if (dirty) {
      this.posBuf.needsUpdate = true
      this.timeBuf.needsUpdate = true
      this.colorAttr.needsUpdate = true
    }
    this.uNow.value = nowRelS
    this.mesh.visible = any
  }

  setWidth(css: number, dpr: number): void {
    this.material.linewidth = Math.max(css, 1 / Math.max(dpr, 1e-3))
  }

  drawCount(): number {
    return this.mesh.visible ? 1 : 0
  }

  dispose(): void {
    this.mesh.geometry.dispose()
    this.material.dispose()
  }
}

export const TRAIL_STYLES = {
  focus: { widthCss: 1, colors: [SCENE.trailDefault, SCENE.glyphPalette[1]], alpha: 1, renderOrder: 40, name: 'TrailFocus' },
  selected: { widthCss: 2, colors: [SCENE.trailSelected, SCENE.glyphPalette[1]], alpha: 1, renderOrder: 42, name: 'TrailSelected' },
  halo: { widthCss: 4, colors: [SCENE.droneHalo], alpha: 0.6, renderOrder: 41, name: 'TrailHalo' },
} as const satisfies Record<string, TrailStyle>
