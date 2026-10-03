// Mission overlay, MissionLayer (M06-FR-044, FR-045, FR-048, AC-034; AWR-15 §10.6; M06 §6.10). Owner: M06.
// Data (from stores/mission.ts and uav/<id>/path through the viewport binding, event driven, rebuilt at <= 4 Hz):
//   executed path 1 px solid g500 and area outlines 1 px dashed g300 -> ThinLineBatch (shared with the GoTo plumb line)
//   planned path 1.5 px dashed g300 (3 / 6)                        -> WideLineBatch (Line2NodeMaterial)
//   task and coverage areas: ground patches (ShapeUtils.triangulateShape, z = DTM + 0.3 m), g50 5 % fill, coverage
//   reveal 10 % -> one Mesh
//   waypoints (planned hollow 10 px, reached solid 6 px g400, current solid 8 px + 14 px ring), formation slots
//   (hollow diamond 10 px g200) and S3 targets (crosshair + ring 16 px; r500 when the red is free) -> GlyphLayer.
// Executed and planned parts are split at the path's done index. The overlay owns three draw objects at most.
import { BufferAttribute, BufferGeometry, DoubleSide, Group, Mesh, ShapeUtils, Sphere, Vector2, Vector3 } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { attribute, uniform, varying, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import type { FrameCtx } from '../loop'
import { GlyphClass, Palette, Shape, type GlyphLayer } from '../drones/glyph/GlyphLayer'
import { GotoMarker } from './gotoMarker'
import { ThinLineBatch, WideLineBatch } from './lineBatch'

type N = any // TSL nodes

/** pts: xyz triples (stride 3) or stores/mission quads (x, y, z, t_rel_s; stride 4, uav/{id}/path polyline4) */
export interface MissionPath { vehicle: string; pts: ArrayLike<number>; doneIdx: number; stride?: 3 | 4 }
export type WaypointState = 'planned' | 'reached' | 'current'
export interface Waypoint { x: number; y: number; z: number; state: WaypointState }
export interface MissionArea { ring: ArrayLike<number>; kind: 'task' | 'coverage' }
export interface MissionData {
  paths: readonly MissionPath[]
  waypoints: readonly Waypoint[]
  areas: readonly MissionArea[]
  slots: readonly { x: number; y: number; z: number }[]
  targets: readonly { x: number; y: number; z: number }[]
}
export const EMPTY_MISSION: MissionData = { paths: [], waypoints: [], areas: [], slots: [], targets: [] }

/**
 * the route and area editor's draft (M15 mission-edit page, AWR-14 §6.8; P4-UI): the draft route as a brighter planned
 * line whose rejected segments are dashed r500 with a warning triangle at their middle, its waypoints as rings (the
 * selected one as the current-waypoint mark), the area being drawn (vertices as diamonds, an open outline until it is
 * closed, then a task patch) and the server preview paths. Interactive: a new draft is drawn in the next frame, without
 * the 4 Hz rebuild cap of the store-driven data.
 */
export interface MissionDraft {
  /** waypoint xyz triples (world ENU m) */
  route: ArrayLike<number>
  /** per segment: 1 = rejected by the coarse check */
  bad: ArrayLike<number> | null
  /** selected waypoint index, -1 none */
  sel: number
  /** area vertices xy pairs and whether the ring is closed */
  area: ArrayLike<number> | null
  areaClosed: boolean
  /** preview paths, xyz triples each */
  preview: readonly ArrayLike<number>[]
}

export const MISSION = { thinCap: 16384, wideCap: 8192, patchTris: 8192, patchLiftM: 0.3, rebuildHz: 4, fillA: 0.05, coverA: 0.1 } as const

export class MissionOverlay {
  readonly root = new Group()
  readonly thin: ThinLineBatch
  readonly planned: WideLineBatch
  readonly patch: Mesh
  readonly goto = new GotoMarker()
  private readonly patchPos: BufferAttribute
  private readonly patchA: BufferAttribute
  private data: MissionData = EMPTY_MISSION
  private draft: MissionDraft | null = null
  /** a draft change rebuilds in the next frame (interactive editing), not at the 4 Hz cap */
  private urgent = false
  private dirty = false
  private lastBuild = Number.NEGATIVE_INFINITY
  private pathSegs = 0
  private groundZ: (x: number, y: number) => number = () => 0

  constructor() {
    this.thin = new ThinLineBatch(MISSION.thinCap, 'MissionLines', 30)
    this.planned = new WideLineBatch(MISSION.wideCap, 1.5, true, 'MissionPlanned', 31)
    const g = new BufferGeometry()
    this.patchPos = new BufferAttribute(new Float32Array(MISSION.patchTris * 9), 3)
    this.patchA = new BufferAttribute(new Float32Array(MISSION.patchTris * 3), 1)
    g.setAttribute('position', this.patchPos)
    g.setAttribute('patchAlpha', this.patchA)
    g.setDrawRange(0, 0)
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    const m = new MeshBasicNodeMaterial()
    const c = SCENE.glyphPalette[0]
    const col: N = uniform(new Vector3(c[0], c[1], c[2]))
    m.colorNode = vec4(col, 1) as N
    m.opacityNode = varying(attribute('patchAlpha', 'float')) as N
    m.transparent = true
    m.depthWrite = false
    m.side = DoubleSide
    m.forceSinglePass = true // one draw per frame: transparent DoubleSide otherwise renders twice (M06-E007; INT-1)
    m.fog = false
    this.patch = new Mesh(g, m)
    this.patch.frustumCulled = false
    this.patch.renderOrder = 29
    this.patch.name = 'MissionAreas'
    this.patch.visible = false
    this.root.add(this.patch, this.thin.obj, this.planned.obj)
    this.root.name = 'MissionLayer'
  }

  setData(d: MissionData): void {
    this.data = d
    this.dirty = true
  }
  /** the editor draft (null when the editor is closed) */
  setDraft(d: MissionDraft | null): void {
    if (d === this.draft) return
    this.draft = d
    this.dirty = true
    this.urgent = true
  }
  /** ground height for area patches (M05 dtm.sample, else coordinate ground.zM) */
  setGround(fn: (x: number, y: number) => number): void {
    this.groundZ = fn
    this.dirty = true
  }
  get mission(): MissionData {
    return this.data
  }

  private rebuild(): void {
    const d = this.data
    const thin = this.thin
    thin.begin()
    this.planned.begin()
    for (const p of d.paths) {
      const k = p.stride ?? 3
      const n = Math.floor(p.pts.length / k)
      let dist = 0
      for (let i = 1; i < n; i++) {
        const ax = p.pts[k * i - k], ay = p.pts[k * i - k + 1], az = p.pts[k * i - k + 2]
        const bx = p.pts[k * i], by = p.pts[k * i + 1], bz = p.pts[k * i + 2]
        if (i <= p.doneIdx) thin.seg(ax, ay, az, bx, by, bz, Palette.G500, false, 1)
        else this.planned.seg(ax, ay, az, bx, by, bz, Palette.G300, dist)
        dist += Math.hypot(bx - ax, by - ay, bz - az)
      }
    }
    // editor draft: preview paths and the route (rejected segments dashed r500 in the thin batch)
    const dr = this.draft
    const areas: { ring: ArrayLike<number>; kind: 'task' | 'coverage' }[] = [...d.areas]
    if (dr) {
      for (const pp of dr.preview) {
        let dist = 0
        for (let i = 3; i + 2 < pp.length; i += 3) {
          this.planned.seg(pp[i - 3], pp[i - 2], pp[i - 1], pp[i], pp[i + 1], pp[i + 2], Palette.G300, dist)
          dist += Math.hypot(pp[i] - pp[i - 3], pp[i + 1] - pp[i - 2], pp[i + 2] - pp[i - 1])
        }
      }
      const r = dr.route
      let dist = 0
      for (let i = 3, k = 0; i + 2 < r.length; i += 3, k++) {
        const ax = r[i - 3], ay = r[i - 2], az = r[i - 1], bx = r[i], by = r[i + 1], bz = r[i + 2]
        if (dr.bad && dr.bad[k]) thin.seg(ax, ay, az, bx, by, bz, Palette.R500, true, 1, dist)
        else this.planned.seg(ax, ay, az, bx, by, bz, Palette.G50, dist)
        dist += Math.hypot(bx - ax, by - ay, bz - az)
      }
      const a = dr.area
      if (a && a.length >= 4) {
        if (dr.areaClosed && a.length >= 6) areas.push({ ring: a, kind: 'task' })
        else {
          for (let i = 2; i + 1 < a.length; i += 2) {
            const zu = this.groundZ(a[i - 2], a[i - 1]) + MISSION.patchLiftM
            const zv = this.groundZ(a[i], a[i + 1]) + MISSION.patchLiftM
            thin.seg(a[i - 2], a[i - 1], zu, a[i], a[i + 1], zv, Palette.G50, false, 1)
          }
        }
      }
    }
    // area outlines (dashed, on the patch height)
    let tri = 0
    const P = this.patchPos.array as Float32Array
    const A = this.patchA.array as Float32Array
    for (const a of areas) {
      const k = Math.floor(a.ring.length / 2)
      if (k < 3) continue
      const contour: Vector2[] = []
      for (let i = 0; i < k; i++) contour.push(new Vector2(a.ring[2 * i], a.ring[2 * i + 1]))
      if (contour[0].equals(contour[k - 1])) contour.pop()
      let dist = 0
      for (let i = 0; i < contour.length; i++) {
        const u = contour[i]
        const v = contour[(i + 1) % contour.length]
        const zu = this.groundZ(u.x, u.y) + MISSION.patchLiftM
        const zv = this.groundZ(v.x, v.y) + MISSION.patchLiftM
        thin.seg(u.x, u.y, zu, v.x, v.y, zv, Palette.G300, true, 1, dist)
        dist += u.distanceTo(v)
      }
      const faces = ShapeUtils.triangulateShape(contour, [])
      const alpha = a.kind === 'coverage' ? MISSION.coverA : MISSION.fillA
      for (const f of faces) {
        if (tri >= MISSION.patchTris) break
        for (let j = 0; j < 3; j++) {
          const q = contour[f[j]]
          const o = 9 * tri + 3 * j
          P[o] = q.x
          P[o + 1] = q.y
          P[o + 2] = this.groundZ(q.x, q.y) + MISSION.patchLiftM
          A[3 * tri + j] = alpha
        }
        tri++
      }
    }
    this.pathSegs = thin.n
    thin.commit(0)
    this.planned.commit()
    this.patch.geometry.setDrawRange(0, 3 * tri)
    this.patch.visible = tri > 0
    this.patchPos.needsUpdate = true
    this.patchA.needsUpdate = true
  }

  /** world phase: rebuild on change (<= 4 Hz), push waypoint glyphs and the GoTo marker */
  update(ctx: FrameCtx, glyphs: GlyphLayer, redAllowed: boolean): void {
    if (this.dirty && (this.urgent || ctx.nowMs - this.lastBuild >= 1000 / MISSION.rebuildHz - 1)) {
      this.dirty = false
      this.urgent = false
      this.lastBuild = ctx.nowMs
      this.rebuild()
    }
    // the GoTo plumb line follows the path segments in the thin batch
    this.thin.n = this.pathSegs
    this.goto.push(glyphs, this.thin, ctx.nowMs, redAllowed)
    this.thin.commit(this.pathSegs)
    this.planned.setWidth(ctx.dpr > 0 ? ctx.dpr : 1)
    this.planned.setView(ctx.dbW, ctx.dbH, (ctx.camera as { near?: number } | null)?.near ?? 0)
    const d = this.data
    for (const w of d.waypoints) {
      if (w.state === 'planned') glyphs.push(GlyphClass.Mission, w.x, w.y, w.z, 10, Shape.Ring, 1.5, Palette.G50)
      else if (w.state === 'reached') glyphs.push(GlyphClass.Mission, w.x, w.y, w.z, 6, Shape.Disc, 1, Palette.G400)
      else {
        glyphs.push(GlyphClass.Mission, w.x, w.y, w.z, 8, Shape.Disc, 1, Palette.G50)
        glyphs.push(GlyphClass.Mission, w.x, w.y, w.z, 14, Shape.Ring, 1.5, Palette.G50)
      }
    }
    for (const s of d.slots) glyphs.push(GlyphClass.Mission, s.x, s.y, s.z, 10, Shape.Diamond, 1.5, Palette.G200)
    const dr = this.draft
    if (dr) {
      const r = dr.route
      for (let i = 0, k = 0; i + 2 < r.length; i += 3, k++) {
        if (k === dr.sel) {
          glyphs.push(GlyphClass.Selection, r[i], r[i + 1], r[i + 2], 8, Shape.Disc, 1, Palette.G50)
          glyphs.push(GlyphClass.Selection, r[i], r[i + 1], r[i + 2], 14, Shape.Ring, 1.5, Palette.G50)
        } else glyphs.push(GlyphClass.Mission, r[i], r[i + 1], r[i + 2], 10, Shape.Ring, 1.5, Palette.G50)
      }
      if (dr.bad) {
        for (let i = 3, k = 0; i + 2 < r.length; i += 3, k++) {
          if (!dr.bad[k]) continue
          glyphs.push(GlyphClass.Warning, (r[i - 3] + r[i]) / 2, (r[i - 2] + r[i + 1]) / 2, (r[i - 1] + r[i + 2]) / 2, 12, Shape.Triangle, 1.5, Palette.R500)
        }
      }
      const a = dr.area
      if (a) for (let i = 0; i + 1 < a.length; i += 2) glyphs.push(GlyphClass.Mission, a[i], a[i + 1], this.groundZ(a[i], a[i + 1]) + MISSION.patchLiftM, 8, Shape.Diamond, 1.5, Palette.G50)
    }
    for (const t of d.targets) glyphs.push(redAllowed ? GlyphClass.Red : GlyphClass.Mission, t.x, t.y, t.z, 16, Shape.Crosshair, 1.5, redAllowed ? Palette.R500 : Palette.G50)
  }

  drawCount(): number {
    return (this.patch.visible ? 1 : 0) + this.thin.drawCount() + this.planned.drawCount()
  }

  dispose(): void {
    this.thin.dispose()
    this.planned.dispose()
    this.patch.geometry.dispose()
    ;(this.patch.material as MeshBasicNodeMaterial).dispose()
  }
}
