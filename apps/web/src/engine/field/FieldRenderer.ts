// Field view renderer (AWR-04 §15.11; ADR-125; WP-11b). Owner: M06.
// One root with two objects: the field points (radiation fields and sensitivity volumes, one draw) and the shells (one
// line draw). World phase, every frame while something is drawn: the anchor table (64 texels) gets the rendered vehicle
// poses and the target positions; the instance data is rebuilt (engine/field/build.ts) at once when the view, the volumes display, the selection,
// the highlight, the budget or the governor level changed, otherwise at most once per second while new values arrived or
// bodies are shown (a target turning re-masks its sector). Nothing per frame allocates; a rebuild uploads only the used
// ranges. Framework-free: the viewport adapter (viewport/layers/field.tsx) supplies the lookups.
import { Group, LineSegments, Mesh, Points, type BufferAttribute, type DataTexture, type InstancedBufferGeometry } from 'three'
import type { PointsNodeMaterial } from 'three/webgpu'
import type { FrameCtx, PointSizeMode } from '../loop'
import type { fieldModel as FieldModel } from '../perception/field'
import { FIELD_LEVEL, type FieldBudget } from './budget'
import { buildField, FIELD, FieldBuffers, type FieldAnchor, type FieldStats, type TargetLookup, type UavLookup } from './build'
import {
  makeAnchorTexture, makeFieldPointsGeometry, makeFieldPointsMaterial, makeFieldShellGeometry, makeFieldShellMaterial, type FieldUniforms,
} from './materials'

export const FIELD_REFRESH = { dataMinMs: 900, periodicMs: 1000 } as const
/** capacities: the largest column of AWR-04 §15.11.5 */
export const FIELD_CAPS = { points: 8192, circles: 96 } as const

export interface FieldSource {
  readonly model: typeof FieldModel
  readonly uavs: UavLookup
  readonly targets: TargetLookup | null
  /** vehicle model of the perception volumes (the primary selection's model) */
  volumeModel(): string | null
  /** changes when vehicle ids may map to other agents (roster version) */
  structureVersion(): number
}

export interface FieldRendererOptions {
  pointSizeMode: PointSizeMode
  createPointsMaterial: () => PointsNodeMaterial
  budget: FieldBudget
}

export class FieldRenderer {
  readonly root = new Group()
  readonly points: Points | Mesh
  readonly shells: LineSegments
  readonly anchors: DataTexture
  readonly mode: 'glpoint' | 'quad'
  readonly buf = new FieldBuffers(FIELD_CAPS.points, FIELD_CAPS.circles)
  private readonly pOff: BufferAttribute
  private readonly pInfo: BufferAttribute
  private readonly sPos: BufferAttribute
  private readonly sInfo: BufferAttribute
  private readonly u: FieldUniforms
  private readonly anc: Float32Array
  private readonly tmp = new Float64Array(3)
  private readonly cam = new Float64Array(3)
  budget: FieldBudget
  budgetId = ''
  /** PerfGovernor level (FIELD_LEVEL) */
  level = 0
  /** a fixed level that the governor does not change (tests; null: the governor's) */
  pinnedLevel: number | null = null
  /** the governor's knob: ignored while a level is pinned */
  setGovernorLevel(l: number): void {
    if (this.pinnedLevel === null) this.level = l
  }
  pinLevel(l: number | null): void {
    this.pinnedLevel = l
    if (l !== null) this.level = l
  }
  /** readout-row highlight (vehicle id) */
  highlight: string | null = null
  private userVisible = true
  private dataVersion = -1
  private lastBuildMs = Number.NEGATIVE_INFINITY
  rebuilds = 0
  lastBuildCpuMs = 0
  /** wall time (performance.now) of the last rebuild after a view change: the switch latency of D2-AC-53 */
  lastSwitchMs = 0

  constructor(o: FieldRendererOptions) {
    this.mode = o.pointSizeMode === 'quad' ? 'quad' : 'glpoint'
    this.budget = o.budget
    this.anchors = makeAnchorTexture()
    this.anc = this.anchors.image.data as Float32Array
    const pg = makeFieldPointsGeometry(this.mode, FIELD_CAPS.points)
    this.pOff = pg.off
    this.pInfo = pg.info
    const pm = makeFieldPointsMaterial(this.mode, o.createPointsMaterial, this.anchors)
    this.u = pm.u
    this.points = this.mode === 'quad' ? new Mesh(pg.geometry, pm.material) : new Points(pg.geometry, pm.material)
    this.points.name = 'FieldPoints'
    this.points.frustumCulled = false
    this.points.renderOrder = 30
    this.points.visible = false
    const sg = makeFieldShellGeometry(this.buf.capVerts)
    this.sPos = sg.pos
    this.sInfo = sg.info
    this.shells = new LineSegments(sg.geometry, makeFieldShellMaterial(this.anchors))
    this.shells.name = 'FieldShells'
    this.shells.frustumCulled = false
    this.shells.renderOrder = 31
    this.shells.visible = false
    this.root.add(this.points, this.shells)
    this.root.name = 'FieldLayer'
  }

  get stats(): FieldStats {
    return this.buf.stats
  }
  get anchorList(): readonly FieldAnchor[] {
    return this.buf.anchors
  }

  setBudget(b: FieldBudget, id: string): void {
    this.budget = b
    this.budgetId = id
  }

  drawCount(): number {
    if (!this.root.visible) return 0
    return (this.points.visible ? 1 : 0) + (this.shells.visible ? 1 : 0)
  }

  setVisible(v: boolean): void {
    this.userVisible = v
    this.root.visible = v
  }

  // the structural inputs of the last build (compared field by field: no key string per frame)
  private sKind: string = ''
  private sVol: string = ''
  private sPhys = false
  private sSel: string | null = null
  private sLevel = -1
  private sBudget = ''
  private sHl: string | null = null
  private sModel: string | null = null
  private sProfile = false
  private sVolumes = false
  private sStruct = -1

  /** world phase: anchors every frame, rebuild when due (nowMs: performance.now) */
  update(ctx: FrameCtx, src: FieldSource, nowMs: number): void {
    const m = src.model
    const on = this.level < FIELD_LEVEL.off && (m.kind !== 'none' || (m.volumesMode !== 'off' && m.volumes !== null))
    this.root.visible = this.userVisible && on
    const vm = src.volumeModel()
    const hasProfile = m.kind !== 'none' && m.profiles.has(m.kind)
    const sv = src.structureVersion()
    const structural = m.kind !== this.sKind || m.volumesMode !== this.sVol || m.physicalRef !== this.sPhys || m.selectedTarget !== this.sSel
      || this.level !== this.sLevel || this.budgetId !== this.sBudget || this.highlight !== this.sHl || vm !== this.sModel || hasProfile !== this.sProfile
      || (m.volumes !== null) !== this.sVolumes || sv !== this.sStruct
    const since = nowMs - this.lastBuildMs
    const due = structural
      || (m.version !== this.dataVersion && since >= FIELD_REFRESH.dataMinMs)
      || (since >= FIELD_REFRESH.periodicMs && on && (this.stats.bodyAnchors > 0 || this.unresolved(m)))
    if (due) {
      this.sKind = m.kind
      this.sVol = m.volumesMode
      this.sPhys = m.physicalRef
      this.sSel = m.selectedTarget
      this.sLevel = this.level
      this.sBudget = this.budgetId
      this.sHl = this.highlight
      this.sModel = vm
      this.sProfile = hasProfile
      this.sVolumes = m.volumes !== null
      this.sStruct = sv
      this.rebuild(ctx, src, structural, nowMs)
    }
    // nothing drawn (no view, no volumes, the layer hidden): the anchor table is not touched (no texture upload per frame)
    if (this.root.visible) this.writeAnchors(src, ctx)
  }

  /** a profile or volumes frame lists anchors that the last build could not place yet */
  private unresolved(m: typeof FieldModel): boolean {
    const st = this.stats
    const f = m.kind !== 'none' ? m.profiles.get(m.kind) : undefined
    const wantU = f ? Math.min(f.uavs.length, FIELD.anchors) : 0
    return st.uavAnchors < wantU || (m.volumesMode !== 'off' && m.volumes !== null && st.bodyAnchors === 0 && m.volumes.targets.size > 0)
  }

  private rebuild(ctx: FrameCtx, src: FieldSource, structural: boolean, nowMs: number): void {
    const t0 = performance.now()
    const m = src.model
    const cam = ctx.camera
    if (cam) {
      // three frame (E, U, -N) -> ENU
      this.cam[0] = cam.position.x
      this.cam[1] = -cam.position.z
      this.cam[2] = cam.position.y
    }
    buildField({
      kind: m.kind, profile: m.kind !== 'none' ? m.profiles.get(m.kind) ?? null : null, volumes: m.volumes, volumesMode: m.volumesMode, physicalRef: m.physicalRef,
      selectedTarget: m.selectedTarget, highlightUav: this.highlight, volumeModel: src.volumeModel(), budget: this.budget, level: this.level,
      camera: cam ? this.cam : null, uavs: src.uavs, targets: src.targets,
    }, this.buf)
    this.dataVersion = m.version
    this.lastBuildMs = nowMs
    this.rebuilds++
    this.upload()
    this.lastBuildCpuMs = performance.now() - t0
    if (structural) this.lastSwitchMs = performance.now()
  }

  private upload(): void {
    const b = this.buf
    const n = b.n
    ;(this.pOff.array as Float32Array).set(b.off.subarray(0, 3 * n))
    ;(this.pInfo.array as Float32Array).set(b.info.subarray(0, 4 * n))
    if (n > 0) {
      this.pOff.clearUpdateRanges()
      this.pOff.addUpdateRange(0, 3 * n)
      this.pInfo.clearUpdateRanges()
      this.pInfo.addUpdateRange(0, 4 * n)
      this.pOff.needsUpdate = true
      this.pInfo.needsUpdate = true
    }
    if (this.mode === 'quad') (this.points.geometry as InstancedBufferGeometry).instanceCount = n
    else this.points.geometry.setDrawRange(0, n)
    this.points.visible = n > 0
    const v = b.nVerts
    ;(this.sPos.array as Float32Array).set(b.sPos.subarray(0, 3 * v))
    ;(this.sInfo.array as Float32Array).set(b.sInfo.subarray(0, 4 * v))
    if (v > 0) {
      this.sPos.clearUpdateRanges()
      this.sPos.addUpdateRange(0, 3 * v)
      this.sInfo.clearUpdateRanges()
      this.sInfo.addUpdateRange(0, 4 * v)
      this.sPos.needsUpdate = true
      this.sInfo.needsUpdate = true
    }
    this.shells.geometry.setDrawRange(0, v)
    this.shells.visible = v > 0
  }

  /** anchor texels: vehicle poses and target positions of this frame (w 0: not placed, its points collapse) */
  private writeAnchors(src: FieldSource, ctx: FrameCtx): void {
    const A = this.anc
    const list = this.buf.anchors
    const t = this.tmp
    for (let s = 0; s < FIELD.anchors; s++) {
      const a = s < list.length ? list[s] : null
      let ok = false
      if (a && a.kind === 'uav') ok = src.uavs.pose(a.agentNo, t)
      else if (a && src.targets) {
        const T = src.targets
        if (a.row < 0 || T.keyOf(a.row) !== a.rowKey) {
          a.row = T.rowOf(a.id)
          a.rowKey = a.row >= 0 ? T.keyOf(a.row) : -1
        }
        if (a.row >= 0) {
          T.pos(a.row, t)
          ok = true
        }
      }
      A[4 * s] = ok ? t[0] : 0
      A[4 * s + 1] = ok ? t[1] : 0
      A[4 * s + 2] = ok ? t[2] : 0
      A[4 * s + 3] = ok ? 1 : 0
    }
    this.anchors.needsUpdate = true
    this.u.dpr.value = ctx.dpr > 0 ? ctx.dpr : 1
    ;(this.u.viewport.value as { set(x: number, y: number): void }).set(Math.max(1, ctx.dbW), Math.max(1, ctx.dbH))
  }

  /** shader-zoo items: one point and one segment drawn with a placed anchor in front of the camera, then restored */
  warmItems(): { object: Points | Mesh | LineSegments; before(): void; after(): void }[] {
    const prev = { n: 0, v: 0 }
    const place = (): void => {
      this.anc[3] = 1
      this.anchors.needsUpdate = true
    }
    return [
      {
        object: this.points,
        before: () => {
          prev.n = this.buf.n
          place()
          if (this.mode === 'quad') (this.points.geometry as InstancedBufferGeometry).instanceCount = Math.max(1, prev.n)
          else this.points.geometry.setDrawRange(0, Math.max(1, prev.n))
          ;(this.pInfo.array as Float32Array)[3] ||= 2
          this.pInfo.needsUpdate = true
          this.points.visible = true
          this.root.visible = true
        },
        after: () => {
          if (this.mode === 'quad') (this.points.geometry as InstancedBufferGeometry).instanceCount = prev.n
          else this.points.geometry.setDrawRange(0, prev.n)
          this.points.visible = prev.n > 0
        },
      },
      {
        object: this.shells,
        before: () => {
          prev.v = this.buf.nVerts
          place()
          this.shells.geometry.setDrawRange(0, Math.max(2, prev.v))
          this.shells.visible = true
          this.root.visible = true
        },
        after: () => {
          this.shells.geometry.setDrawRange(0, prev.v)
          this.shells.visible = prev.v > 0
        },
      },
    ]
  }

  dispose(): void {
    this.points.geometry.dispose()
    ;(this.points.material as { dispose(): void }).dispose()
    this.shells.geometry.dispose()
    ;(this.shells.material as { dispose(): void }).dispose()
    this.anchors.dispose()
  }
}
