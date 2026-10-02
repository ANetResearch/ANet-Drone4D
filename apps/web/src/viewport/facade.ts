// Viewport facade for ui/** (M06 §7.1; AWR-14 §3.4, §6.4-§6.7). Owner: M06. ui/** reaches the viewport only here.
// viewport: unobscured rect (projection centre, tweened), frame cap and suspension, projection to screen, rebuild;
// camera: modes 1-5 with guards, follow lock, focus, north up, home, ViewCube directions, pose read/write;
// pick: click/hover/preview picking and the ground pick of the last click; mission: GoTo and single-vehicle commands,
// GoTo marker state; drones: highlights and the red owner (normally the viewport RedArbiter binding); layers: visibility;
// labels: M15 text formatter; governor and registry extension points. Signatures only grow (M15, SK-E2E callers).
import { events, loop, perf, projectEnu, type CameraPose, type GotoState, type GovernorKnob, type PickOptions, type PickResult } from '@/engine'
import type { CallHandle, CallStatus } from '@/net/rt'
import { selectionStore } from '@/stores/selection'
import { vp, type PointPickInfo } from './session'
import { clickAt, pointInfoAt } from './interaction'
import { sendGoto, gotoResults, gotoTargetFor, primaryAgentNo, sendVehicleCommand, vehicleCmdState, type GotoOptions, type GotoRequest, type VehicleCmd, type VehicleCmdState } from './gotoRule'
import { getLayer, registerLayer, type LayerId, type LayerSpec } from './layers/registry'
import { requestRebuild } from './hostRuntime'
import { setRedOverride } from './bindings/redOwner'
import { agentNoOf } from './bindings/selection'
import { setLabelFormatter, type LabelFormatter } from './overlay/labelFormatter'
import { viewCube as viewCubeApi } from './overlay/ViewCube'
import { skyControl } from './layers/groundSky'
import { Vector3 } from 'three'
import type { SensorsApi } from '@/engine'

export type { GotoOptions, GotoRoute, VehicleCmd, VehicleCmdState } from './gotoRule'
export type { CameraPose } from '@/engine'
export type { LabelFormatter } from './overlay/labelFormatter'

export interface Rect { x: number; y: number; w: number; h: number }
export type CameraMode = 'orbit' | 'free' | 'third' | 'fpv' | 'bird'

const unobscured: Rect = { x: 0, y: 0, w: 0, h: 0 }
const tmp = new Vector3()
let pendingMode: CameraMode = 'orbit'
let pendingLock = false

export const viewport = {
  /** CSS px; the camera tweens its projection centre to the rect centre (FR-051); the drawing buffer never changes */
  setUnobscuredRect(r: Rect, t?: { durationMs: number; ease: 'smooth-out' }): void {
    unobscured.x = r.x
    unobscured.y = r.y
    unobscured.w = r.w
    unobscured.h = r.h
    const W = globalThis.innerWidth || vp.cssW
    const H = globalThis.innerHeight || vp.cssH
    if (r.w > 0 && r.h > 0 && W > 0 && H > 0) vp.rig?.setViewCentre(r.x + r.w / 2, r.y + r.h / 2, W, H, t?.durationMs ?? 0)
    else vp.rig?.setViewCentre(null, 0, 0, 0)
  },
  getUnobscuredRect(): Readonly<Rect> {
    return unobscured
  },
  /** 0 = uncapped (modal 15, overlay pages 5) */
  setFrameCap(fps: number): void {
    loop.setFrameCap(fps)
  },
  setSuspended(on: boolean): void {
    loop.setSuspended(on)
  },
  /** writes CSS px (origin top-left of the viewport) into out; false when the point is outside the frustum */
  projectToScreen(enu: ArrayLike<number>, out: Float32Array): boolean {
    const cam = vp.camera
    if (!cam || vp.cssW <= 0) return false
    return projectEnu(cam, vp.cssW, vp.cssH, enu, out, tmp)
  },
  /** screen anchor of a vehicle id (Popover VirtualElement), false when unknown or outside the view */
  anchor(id: string, out: Float32Array): boolean {
    const a = agentNoOf(id)
    const p = new Float64Array(3)
    return a >= 0 && !!vp.drones?.poseOf(a, p) && viewport.projectToScreen(p, out)
  },
  /** the viewport state changed (backend ready, world opened, pick, goto marker) */
  onChange(cb: () => void): () => void {
    return vp.subscribe(cb)
  },
  /** device-loss rebuild path triggered by the user (error overlay E-14); no re-download */
  rebuild(): Promise<void> {
    return requestRebuild('user')
  },
  get backend(): { tier: string; deviceClass: string; state: string } | null {
    return vp.be ? { tier: vp.be.tier, deviceClass: vp.be.deviceClass, state: vp.be.state } : null
  },
  get worldId(): string | null {
    return vp.world?.worldId ?? null
  },
  /** M13 sensor API injection (when M13 wires it explicitly instead of engine/sensors discovery) */
  setSensors(api: SensorsApi | null): void {
    vp.sensors = api
    vp.drones?.setSensors(api)
  },
  /** M07: horizon (= fog) colour, optional zenith, linear sRGB */
  setSkyColors(horizon: readonly number[], zenith?: readonly number[]): void {
    skyControl.setHorizon(horizon, zenith)
  },
}

/** LoopDriver: re-apply what ui/** set before the camera rig existed (unobscured rect, mode, follow lock) */
export function applyPendingViewport(): void {
  if (unobscured.w > 0 && unobscured.h > 0) viewport.setUnobscuredRect(unobscured)
  if (pendingMode !== 'orbit' && vp.rig && vp.rig.mode === 'orbit') camera.setMode(pendingMode)
  if (pendingLock && vp.rig) camera.setFollowLock(true)
}

export const camera = {
  setMode(m: CameraMode): { ok: boolean; reason?: 'no_focus' | 'no_camera_sensor' } {
    const focus = primaryAgentNo()
    if (!vp.rig) {
      if ((m === 'third' || m === 'fpv') && focus < 0) return { ok: false, reason: 'no_focus' }
      pendingMode = m
      return { ok: true }
    }
    return vp.rig.setMode(m, m === 'third' || m === 'fpv' ? focus : vp.rig.focusAgent)
  },
  get mode(): CameraMode {
    return vp.rig?.mode ?? pendingMode
  },
  setFollowLock(on: boolean): boolean {
    if (!vp.rig) {
      pendingLock = on
      return true
    }
    return vp.rig.setFollowLock(on, primaryAgentNo())
  },
  get followLock(): boolean {
    return vp.rig?.followLock ?? pendingLock
  },
  /** fitToBox of the selection (single vehicle: 60 m sphere, padding 0.1); empty: world bounds */
  focus(ids?: readonly string[]): void {
    const rig = vp.rig
    if (!rig) return
    const list = ids && ids.length ? ids : selectionStore.getState().ids
    const p = new Float64Array(3)
    let n = 0
    const lo = [Infinity, Infinity, Infinity]
    const hi = [-Infinity, -Infinity, -Infinity]
    for (const id of list) {
      const a = agentNoOf(id)
      if (a < 0 || !vp.drones?.poseOf(a, p)) continue
      n++
      for (let k = 0; k < 3; k++) {
        lo[k] = Math.min(lo[k], p[k])
        hi[k] = Math.max(hi[k], p[k])
      }
    }
    if (n > 0) {
      const c = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2]
      const r = Math.max(60, Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2)
      rig.focusSphere(c, r)
      return
    }
    const w = vp.worldCtx
    if (w?.boundsMin && w.boundsMax) {
      const c = [(w.boundsMin[0] + w.boundsMax[0]) / 2, (w.boundsMin[1] + w.boundsMax[1]) / 2, (w.boundsMin[2] + w.boundsMax[2]) / 2]
      rig.focusSphere(c, Math.hypot(w.boundsMax[0] - w.boundsMin[0], w.boundsMax[1] - w.boundsMin[1]) / 2)
    } else rig.goHome()
  },
  northUp(): void {
    vp.rig?.northUp()
  },
  home(): void {
    vp.rig?.goHome()
  },
  /** ViewCube: look from direction n (ENU) keeping target and distance */
  viewFrom(n: readonly [number, number, number]): void {
    vp.rig?.viewFrom(n)
  },
  getPose(): CameraPose | null {
    return vp.rig?.getPose() ?? null
  },
  setPose(p: CameraPose, o?: { fly: boolean }): void {
    vp.rig?.setPose(p, o)
  },
  onMode(cb: (m: { mode: CameraMode; followLock: boolean }) => void): () => void {
    return events.on<{ mode: CameraMode; followLock: boolean }>('camera.mode', cb)
  },
  /** camera motion start (active true) and rest (false): GoTo confirm popover closes on movement (M15 §6.9) */
  onMoved(cb: (m: { active: boolean }) => void): () => void {
    return events.on<{ active: boolean }>('camera.moved', cb)
  },
}

/** fed by stores/selection through bindings/selection.ts; kept for callers that push highlights directly */
export const drones = {
  setHighlights(h: { selected: readonly string[]; primary: string | null; hover: string | null }): void {
    vp.drones?.layer.setHighlights(h.selected.map(agentNoOf).filter((a) => a >= 0), agentNoOf(h.primary), agentNoOf(h.hover))
  },
  /** explicit red owner (null returns to the viewport RedArbiter) */
  setRedOwner(o: { kind: 'drone' | 'zone' | 'target' | 'class'; id: string } | null): void {
    setRedOverride(o)
  },
}

export interface GroundPickView { worldId: string; surface: [number, number, number]; target: [number, number, number] | null }

export const pick = {
  /** last ground pick of a viewport click, with the GoTo target for the primary selection */
  get ground(): GroundPickView | null {
    const p = vp.pick
    if (!p) return null
    const no = primaryAgentNo()
    return { worldId: p.worldId, surface: [p.surface[0], p.surface[1], p.surface[2]], target: no >= 0 ? gotoTargetFor(p.surface, no) : null }
  },
  /** last point-cloud point under a click (ENU, class, HAG; PRD-FR-017, D1-ext), null before a hit */
  get point(): PointPickInfo | null {
    return vp.pointPick
  },
  /** point-cloud pick at CSS px without the click side effects (the ID pass result, or null on a miss) */
  pointAt(cssX: number, cssY: number): Promise<PointPickInfo | null> {
    return pointInfoAt(cssX, cssY)
  },
  /** the click path at CSS px of the viewport (selects a vehicle or sets the ground pick; tests use it too) */
  at(cssX: number, cssY: number): Promise<'drone' | 'ground' | 'none'> {
    return clickAt(cssX, cssY, vp.cssW, vp.cssH)
  },
  /** picking in `want` order: drone -> point (ext) -> ground (M06 §6.12) */
  pickAt(cssX: number, cssY: number, opts: PickOptions): Promise<PickResult> {
    return vp.picker ? vp.picker.pickAt(cssX, cssY, opts) : Promise.resolve({ kind: 'none' })
  },
  /** hover pick (<= 20 Hz, none while the camera moves); results also go out as 'pick.hover' */
  hover(cssX: number, cssY: number): PickResult | null {
    return vp.picker?.hoverAt(cssX, cssY) ?? null
  },
  /** GoTo tool preview: ray_hit at <= 5 Hz, previous request aborted */
  async previewGround(cssX: number, cssY: number): Promise<PickResult> {
    if (!vp.picker) return { kind: 'none' }
    const g = await vp.picker.previewGround(cssX, cssY)
    return g.kind === 'ground' ? g : { kind: 'none', reason: g.reason }
  },
  clear(): void {
    vp.pick = null
    vp.pointPick = null
    vp.mission?.goto.clear()
    vp.changed()
  },
}

export const mission = {
  /** goto for the primary selection to the ground pick; null when a precondition is missing */
  sendGoto(o?: GotoOptions): GotoRequest | null {
    return sendGoto(o)
  },
  get gotoState(): GotoState | null {
    return vp.mission?.goto.state ?? null
  },
  /** result statuses of the last goto in arrival order */
  get gotoResults(): readonly string[] {
    return gotoResults
  },
  /** takeoff, hover, land or rtl for the primary selection (`uav/{id}/cmd/{op}`); null without a primary */
  command(op: VehicleCmd, args?: Record<string, unknown>): CallHandle | null {
    return sendVehicleCommand(op, args)
  },
  /** last result of op for the primary selection; viewport.onChange fires on every result */
  commandState(op: VehicleCmd): VehicleCmdState | null {
    return vehicleCmdState(op)
  },
  /** GoTo tool preview marker at a surface point (null hides it) */
  setGotoPreview(p: Float64Array | null): void {
    const g = vp.mission?.goto
    if (!g) return
    if (!p) g.clear()
    else g.set(p, gotoTargetFor(p, primaryAgentNo()), 'preview', performance.now())
    vp.changed()
  },
  /** GoTo marker state from a call status (M15 tool state machine) */
  setGotoState(_callId: string, s: CallStatus): void {
    const g = vp.mission?.goto
    if (!g) return
    const now = performance.now()
    g.setState(s === 'accepted' ? 'accepted' : s === 'running' ? 'running' : s === 'succeeded' ? 'succeeded' : 'failed', now)
    vp.changed()
  },
}

export const layers = {
  setVisible(id: LayerId, v: boolean): void {
    getLayer(id)?.setVisible(v)
  },
}

export const labels = {
  /** M15 injects the localised FlightState short texts */
  setFormatter(fn: LabelFormatter | null): void {
    setLabelFormatter(fn)
  },
}

export const viewCube = viewCubeApi

export const governor = {
  registerKnob(k: GovernorKnob): () => void {
    return perf.registerKnob(k)
  },
}

export const registry = {
  register(spec: LayerSpec): () => void {
    return registerLayer(spec)
  },
}
