// Picker: pick orchestration (M06 §6.12, FR-061..065, AC-043..046; AWR-14 §6.6, §6.7). Owner: M06.
// pickAt(x, y, {want}) resolves in the order drone (CPU ray-sphere, synchronous) -> point (M05 ID pass, D1-ext, when a
// point picker is attached) -> ground (M04 ray_hit); only the kinds in `want` are tried. hoverAt(x, y) runs at most at
// input.hoverPickHz (20 Hz) and never while the camera moves (controls active or flying); results go out as 'pick.hover'.
// The screen ray uses the camera's current projection (view offset included) and is converted to world ENU.
// projectToScreen(enu) maps world ENU to CSS px of the viewport (M06-FR-065). Everything asynchronous; no long tasks.
import { Raycaster, Vector2, Vector3, type PerspectiveCamera } from 'three'
import { INPUT } from '@/lib/tokens/input.gen'
import { events } from '../loop'
import type { DronePoseSoA } from '../time/interpRing'
import { threeToEnu, enuToThree } from '../camera/CameraRig'
import { pickDroneRay, type DronePickHit } from './dronePick'
import { GroundRay, type GroundResult, type QueryFn } from './groundRay'
import { hypot3 } from '../hypot'

export type PickKind = 'drone' | 'point' | 'ground'
export type PickResult =
  | { kind: 'drone'; id: string; agentNo: number; distM: number; pointEnu: Float64Array }
  | { kind: 'point'; nodeId: number; pointEnu: Float64Array; classIdx: number; className?: string; hagM?: number | null; normal?: Float64Array | null; spacingM?: number }
  | { kind: 'ground'; pointEnu: Float64Array; surface: string; distM: number }
  | { kind: 'none'; reason?: string }
export interface PickOptions { want: readonly PickKind[]; signal?: AbortSignal }

export interface PickerDeps {
  camera: () => PerspectiveCamera | null
  size: () => { w: number; h: number }
  poses: () => DronePoseSoA | null
  idOf: (agentNo: number) => string
  worldId: () => string | null
  /** true while the camera moves (no hover picking) */
  moving: () => boolean
  /** M05 point picker (D1-ext); null until delivered */
  pickPoint?: (cssX: number, cssY: number, o: Float64Array, d: Float64Array) => Promise<PickResult | null>
  /** per-index hidden flags (FPV focus vehicle) */
  hidden?: () => Uint8Array | null
  query?: QueryFn
  now?: () => number
}

export const PICK = { hotZoneCssPx: 6, rVisM: 0.6, maxRangeM: 5000, timeoutMs: 1000 } as const

export class Picker {
  private readonly ray = new Raycaster()
  private readonly ndc = new Vector2()
  private readonly v = new Vector3()
  readonly origin = new Float64Array(3)
  readonly dir = new Float64Array(3)
  readonly ground: GroundRay
  private readonly hit: DronePickHit = { index: -1, t: 0 }
  private lastHoverMs = Number.NEGATIVE_INFINITY
  hoverCount = 0
  lastHover: PickResult = { kind: 'none' }

  constructor(private readonly deps: PickerDeps) {
    this.ground = new GroundRay(deps.query, deps.now)
  }

  get requests(): number {
    return this.ground.requests
  }

  /** ENU ray through a CSS pixel of a cssW x cssH viewport */
  rayAt(cssX: number, cssY: number, camera: PerspectiveCamera, cssW: number, cssH: number): void {
    this.ndc.set((cssX / cssW) * 2 - 1, -((cssY / cssH) * 2 - 1))
    if (camera.matrixAutoUpdate) camera.updateMatrixWorld()
    this.ray.setFromCamera(this.ndc, camera)
    threeToEnu(this.ray.ray.origin, this.origin)
    threeToEnu(this.ray.ray.direction, this.dir)
    const l = hypot3(this.dir[0], this.dir[1], this.dir[2]) || 1
    this.dir[0] /= l
    this.dir[1] /= l
    this.dir[2] /= l
  }

  /** nearest drone on the current ray (index into poses) or -1 */
  pickDrone(poses: DronePoseSoA, fovDeg: number, cssH: number, near: number): { index: number; t: number } {
    const k = (2 * Math.tan((fovDeg * Math.PI) / 360)) / Math.max(1, cssH)
    return pickDroneRay(this.origin, this.dir, poses, k, near, this.hit, PICK.rVisM, this.deps.hidden?.() ?? null)
  }

  async pickAt(cssX: number, cssY: number, opts: PickOptions): Promise<PickResult> {
    const cam = this.deps.camera()
    const { w, h } = this.deps.size()
    if (!cam || w <= 0 || h <= 0) return { kind: 'none', reason: 'no-camera' }
    this.rayAt(cssX, cssY, cam, w, h)
    const o = Float64Array.from(this.origin)
    const d = Float64Array.from(this.dir)
    for (const kind of opts.want) {
      if (opts.signal?.aborted) return { kind: 'none', reason: 'aborted' }
      if (kind === 'drone') {
        const poses = this.deps.poses()
        if (!poses) continue
        const hh = this.pickDrone(poses, cam.fov, h, cam.near)
        if (hh.index >= 0) {
          const a = poses.agentNo[hh.index]
          const p = Float64Array.of(o[0] + d[0] * hh.t, o[1] + d[1] * hh.t, o[2] + d[2] * hh.t)
          return { kind: 'drone', id: this.deps.idOf(a), agentNo: a, distM: hh.t, pointEnu: p }
        }
      } else if (kind === 'point' && this.deps.pickPoint) {
        const r = await this.deps.pickPoint(cssX, cssY, o, d)
        if (r && r.kind === 'point') return r
      } else if (kind === 'ground') {
        const wid = this.deps.worldId()
        if (!wid) continue
        const g: GroundResult = await this.ground.hit(wid, o, d, opts.signal)
        if (g.kind === 'ground') return g
        return { kind: 'none', reason: g.reason }
      }
    }
    return { kind: 'none', reason: 'miss' }
  }

  /** hover pick at <= 20 Hz, off while the camera moves; emits 'pick.hover' when the result changes */
  hoverAt(cssX: number, cssY: number): PickResult | null {
    const now = this.deps.now ? this.deps.now() : performance.now()
    if (now - this.lastHoverMs < 1000 / INPUT.hoverPickHz || this.deps.moving()) return null
    this.lastHoverMs = now
    const cam = this.deps.camera()
    const poses = this.deps.poses()
    const { w, h } = this.deps.size()
    if (!cam || !poses || w <= 0) return null
    this.rayAt(cssX, cssY, cam, w, h)
    const hh = this.pickDrone(poses, cam.fov, h, cam.near)
    this.hoverCount++
    const prev = this.lastHover
    if (hh.index >= 0) {
      const a = poses.agentNo[hh.index]
      if (prev.kind === 'drone' && prev.agentNo === a) return prev
      this.lastHover = { kind: 'drone', id: this.deps.idOf(a), agentNo: a, distM: hh.t, pointEnu: Float64Array.of(this.origin[0] + this.dir[0] * hh.t, this.origin[1] + this.dir[1] * hh.t, this.origin[2] + this.dir[2] * hh.t) }
    } else {
      if (prev.kind === 'none') return prev
      this.lastHover = { kind: 'none' }
    }
    events.emit('pick.hover', this.lastHover)
    return this.lastHover
  }

  /** GoTo preview: ray_hit at <= 5 Hz with the previous request aborted */
  async previewGround(cssX: number, cssY: number): Promise<GroundResult> {
    const cam = this.deps.camera()
    const { w, h } = this.deps.size()
    const wid = this.deps.worldId()
    if (!cam || !wid || w <= 0) return { kind: 'none', reason: 'error' }
    this.rayAt(cssX, cssY, cam, w, h)
    return this.ground.preview(wid, this.origin, this.dir)
  }

  /** world ENU -> CSS px (origin top-left); false outside the view frustum */
  projectToScreen(enu: ArrayLike<number>, out: Float32Array | Float64Array): boolean {
    const cam = this.deps.camera()
    const { w, h } = this.deps.size()
    if (!cam || w <= 0) return false
    return projectEnu(cam, w, h, enu, out, this.v)
  }
}

/** world ENU -> CSS px through the camera (projection includes the view offset) */
export function projectEnu(cam: PerspectiveCamera, cssW: number, cssH: number, enu: ArrayLike<number>, out: Float32Array | Float64Array, tmp: Vector3): boolean {
  enuToThree(enu[0], enu[1], enu[2], tmp).applyMatrix4(cam.matrixWorldInverse)
  const front = -tmp.z >= cam.near && -tmp.z <= cam.far
  tmp.applyMatrix4(cam.projectionMatrix)
  out[0] = ((tmp.x + 1) / 2) * cssW
  out[1] = ((1 - tmp.y) / 2) * cssH
  return front && Math.abs(tmp.x) <= 1 && Math.abs(tmp.y) <= 1
}
