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
    // area outlines (dashed, on the patch height)
    let tri = 0
    const P = this.patchPos.array as Float32Array
    const A = this.patchA.array as Float32Array
    for (const a of d.areas) {
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
    if (this.dirty && ctx.nowMs - this.lastBuild >= 1000 / MISSION.rebuildHz - 1) {
      this.dirty = false
      this.lastBuild = ctx.nowMs
      this.rebuild()
    }
    // the GoTo plumb line follows the path segments in the thin batch
    this.thin.n = this.pathSegs
    this.goto.push(glyphs, this.thin, ctx.nowMs, redAllowed)
    this.thin.commit(this.pathSegs)
    this.planned.setWidth(ctx.dpr > 0 ? ctx.dpr : 1)
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
