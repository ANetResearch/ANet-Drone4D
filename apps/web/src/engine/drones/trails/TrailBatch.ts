// GPU trail batch (M06-FR-042, AC-033; M06 §6.10; AWR-15 §10.5). Owner: M06.
// One Mesh per batch (focus set 1 px g400, selected 2 px g50 or r500, selected halo 4 px --drone-halo 60 %); every batch
// owns its geometry (M06 §6.3 rule 11). Screen-space wide lines without instancing (engine/lines/quadLines.ts; FX2-R3,
// ADR-067): every segment is an explicit quad whose extras are the segment's start and end times (sim seconds relative
// to the trail block, -1 for an empty segment, which the vertex stage collapses) and whose palette index selects the
// colour. Slot s owns segments [s segs, (s + 1) segs) as a ring; the draw range ends at the last occupied slot. The age
// alpha a = mix(0.8, 0.15, clamp((uNow - t) / 120 s, 0, 1)) is evaluated from uNow = tRender - blockStart, so the
// geometry is not rebuilt per frame; beyond the window the fragment is discarded. Widths are CSS px, raster >= 1 px.
// The previous LineSegments2 + Line2NodeMaterial batch drew every allocated segment as an instance; on SwiftShader the
// selected trail and its halo (2 x 1024 instances, mostly empty) cost about 180 ms of GPU-process CPU per frame, the
// frame interval went from 50 to 100 ms on selecting a vehicle (D1-AC-26).
import { DoubleSide, Mesh, NormalBlending, Vector3, type BufferGeometry, type InterleavedBuffer } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, clamp, float, mix, select, uniform } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import {
  flushQuadLines, makeQuadLineGeometry, makeQuadLineUniforms, quadLineAttributes, quadLineVertexNode, setQuadLineView, writeQuadLine,
  writeQuadLineColor, writeQuadLineExtra, type QuadLineUniforms,
} from '../../lines/quadLines'
import { TRAIL, type TrailRing } from './TrailRing'

type N = any // TSL nodes

export interface TrailStyle { widthCss: number; colors: readonly (readonly [number, number, number])[]; alpha: number; renderOrder: number; name: string }

export class TrailBatch {
  readonly mesh: Mesh
  private readonly data: Float32Array
  private readonly buf: InterleavedBuffer
  private readonly geometry: BufferGeometry
  private readonly material: MeshBasicNodeMaterial
  private readonly uNow: N
  private readonly u: QuadLineUniforms
  /** agent per slot (-1 empty), last synced ring sequence, write cursor */
  readonly slotAgent: Int32Array
  private readonly slotSeq: Float64Array
  private readonly slotCursor: Int32Array
  private readonly slotColor: Float32Array
  private readonly tmp = new Float32Array(8)
  /** dirty segment ranges of this frame [lo, hi] (merged when adjacent), uploaded with addUpdateRange */
  private readonly dirtyLo: Int32Array
  private readonly dirtyHi: Int32Array
  private dirtyN = 0
  private fullUpload = true
  private widthCss: number
  private dpr = 1
  segs: number
  activeSlots: number

  constructor(readonly maxSlots: number, readonly maxSegs: number, readonly style: TrailStyle) {
    this.segs = maxSegs
    this.activeSlots = maxSlots
    this.widthCss = style.widthCss
    const nSeg = maxSlots * maxSegs
    const q = makeQuadLineGeometry(nSeg)
    this.data = q.data
    this.buf = q.buf
    this.geometry = q.geometry
    for (let i = 0; i < nSeg; i++) writeQuadLineExtra(this.data, i, -1, -1)
    this.uNow = uniform(0)
    this.u = makeQuadLineUniforms()
    const m = new MeshBasicNodeMaterial({ side: DoubleSide })
    const cols = style.colors.map((c) => uniform(new Vector3(c[0], c[1], c[2])))
    const a = quadLineAttributes()
    const tAt: N = a.extraAt
    m.vertexNode = quadLineVertexNode(this.u, (e: N) => e.x.greaterThanEqual(0)) as N
    const age: N = this.uNow.sub(tAt)
    m.opacityNode = Fn(() => {
      tAt.lessThan(0).or(age.greaterThan(TRAIL.windowS)).discard()
      return mix(float(0.8), float(0.15), clamp(age.div(TRAIL.windowS), float(0), float(1))).mul(style.alpha)
    })() as N
    let c: N = cols[cols.length - 1]
    for (let i = cols.length - 2; i >= 0; i--) c = select(a.colorIdx.lessThan(i + 0.5), cols[i], c)
    m.colorNode = c
    m.transparent = true
    m.blending = NormalBlending
    m.depthWrite = false
    m.depthTest = true
    m.fog = false
    // three draws transparent double-sided materials twice unless single pass (pass plan, INT-1)
    m.forceSinglePass = true
    this.material = m
    this.mesh = new Mesh(this.geometry, m)
    this.mesh.frustumCulled = false
    this.mesh.renderOrder = style.renderOrder
    this.mesh.name = style.name
    this.mesh.visible = false
    this.slotAgent = new Int32Array(maxSlots).fill(-1)
    this.slotSeq = new Float64Array(maxSlots)
    this.slotCursor = new Int32Array(maxSlots)
    this.slotColor = new Float32Array(maxSlots)
    this.dirtyLo = new Int32Array(2 * maxSlots + 2)
    this.dirtyHi = new Int32Array(2 * maxSlots + 2)
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
    for (let i = 0; i < this.maxSlots * this.maxSegs; i++) writeQuadLineExtra(this.data, i, -1, -1)
    this.slotAgent.fill(-1)
    for (let i = 0; i < agents.length; i++) this.assign(i, agents[i], colors[i] ?? 0, ring)
    this.fullUpload = true
  }

  /** set the slot's vehicle and copy its CPU history (<= segs segments) */
  assign(slot: number, agentNo: number, colorIdx: number, ring: TrailRing): void {
    this.slotAgent[slot] = agentNo
    this.slotColor[slot] = colorIdx
    const base = slot * this.segs
    for (let j = 0; j < this.segs; j++) writeQuadLineExtra(this.data, base + j, -1, -1)
    this.slotCursor[slot] = 0
    const r = ring.rowFor(agentNo)
    this.slotSeq[slot] = r >= 0 ? ring.seq[r] : 0
    if (r >= 0) {
      const c = ring.count[r]
      const first = Math.max(1, c - this.segs)
      for (let j = first; j < c; j++) {
        ring.sample(r, j - 1, this.tmp, 0)
        ring.sample(r, j, this.tmp, 4)
        this.writeSeg(slot, this.tmp, colorIdx)
      }
    }
    this.markDirty(base, base + this.segs - 1)
  }

  release(slot: number): void {
    const base = slot * this.segs
    for (let j = 0; j < this.segs; j++) writeQuadLineExtra(this.data, base + j, -1, -1)
    this.markDirty(base, base + this.segs - 1)
    this.slotAgent[slot] = -1
  }

  private writeSeg(slot: number, s: Float32Array, colorIdx: number): number {
    const i = slot * this.segs + this.slotCursor[slot]
    this.slotCursor[slot] = (this.slotCursor[slot] + 1) % this.segs
    writeQuadLine(this.data, i, s[0], s[1], s[2], s[4], s[5], s[6], s[3], s[7], colorIdx)
    return i
  }

  /** remember a dirty segment range, merged with the previous one when adjacent (at most 2 per slot) */
  private markDirty(lo: number, hi: number): void {
    const n = this.dirtyN
    if (n > 0 && lo <= this.dirtyHi[n - 1] + 1 && hi >= this.dirtyLo[n - 1] - 1) {
      this.dirtyLo[n - 1] = Math.min(lo, this.dirtyLo[n - 1])
      this.dirtyHi[n - 1] = Math.max(hi, this.dirtyHi[n - 1])
      return
    }
    if (n >= this.dirtyLo.length) {
      this.fullUpload = true
      return
    }
    this.dirtyLo[n] = lo
    this.dirtyHi[n] = hi
    this.dirtyN = n + 1
  }

  /** per frame: append the new CPU samples of the slotted vehicles; recolour on selection or red-owner changes */
  sync(ring: TrailRing, nowRelS: number, colorOf: (agentNo: number) => number): void {
    let any = false
    let last = -1
    for (let s = 0; s < this.activeSlots; s++) {
      const a = this.slotAgent[s]
      if (a < 0) continue
      any = true
      last = s
      const col = colorOf(a)
      const base = s * this.segs
      if (col !== this.slotColor[s]) {
        this.slotColor[s] = col
        writeQuadLineColor(this.data, base, base + this.segs, col)
        this.markDirty(base, base + this.segs - 1)
      }
      const r = ring.rowFor(a)
      if (r < 0) continue
      const seq = ring.seq[r]
      const add = Math.min(seq - this.slotSeq[s], ring.count[r] - 1, this.segs)
      for (let k = add; k >= 1; k--) {
        const c = ring.count[r]
        ring.sample(r, c - k - 1, this.tmp, 0)
        ring.sample(r, c - k, this.tmp, 4)
        const i = this.writeSeg(s, this.tmp, col)
        this.markDirty(i, i)
      }
      this.slotSeq[s] = seq
    }
    if (this.fullUpload) flushQuadLines(this.buf, null)
    else if (this.dirtyN > 0) flushQuadLines(this.buf, this.dirtyLo, this.dirtyHi, this.dirtyN)
    this.fullUpload = false
    this.dirtyN = 0
    this.uNow.value = nowRelS
    this.geometry.setDrawRange(0, (last + 1) * this.segs * 6)
    this.mesh.visible = any
  }

  /** line width in CSS px and the raster/CSS ratio; the raster width is at least 1 px */
  setWidth(css: number, dpr: number): void {
    this.widthCss = css
    this.dpr = dpr
    this.u.widthPx.value = Math.max(css * Math.max(dpr, 1e-3), 1)
  }

  /** drawing-buffer size in px and the camera near plane (m), once per frame */
  setView(dbW: number, dbH: number, near: number): void {
    setQuadLineView(this.u, this.widthCss, this.dpr, dbW, dbH, near)
  }

  /** shader zoo warm-up: one (empty) segment drawn; restored by the next sync() */
  warmBefore(): void {
    this.geometry.setDrawRange(0, 6)
  }

  drawCount(): number {
    return this.mesh.visible ? 1 : 0
  }

  dispose(): void {
    this.geometry.dispose()
    this.material.dispose()
  }
}

export const TRAIL_STYLES = {
  focus: { widthCss: 1, colors: [SCENE.trailDefault, SCENE.glyphPalette[1]], alpha: 1, renderOrder: 40, name: 'TrailFocus' },
  selected: { widthCss: 2, colors: [SCENE.trailSelected, SCENE.glyphPalette[1]], alpha: 1, renderOrder: 42, name: 'TrailSelected' },
  halo: { widthCss: 4, colors: [SCENE.droneHalo], alpha: 0.6, renderOrder: 41, name: 'TrailHalo' },
} as const satisfies Record<string, TrailStyle>
