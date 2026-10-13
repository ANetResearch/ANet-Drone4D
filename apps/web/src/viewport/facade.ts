// Viewport facade for ui/** (M06 §7.1; AWR-14 §3.4, §6.4-§6.7). Owner: M06. ui/** reaches the viewport only here.
// viewport: unobscured rect (projection centre, tweened), frame cap and suspension, projection to screen, rebuild;
// camera: modes 1-5 with guards, follow lock, focus, north up, home, ViewCube directions, pose read/write;
// pick: click/hover/preview picking and the ground pick of the last click; mission: GoTo and single-vehicle commands,
// GoTo marker state; drones: highlights and the red owner (normally the viewport RedArbiter binding); layers: visibility;
// labels: M15 text formatter; governor and registry extension points; D2 (WP-11): sensorView (FPV, EO, night and LWIR
// views of the FPV camera, sensor choice, palette, digital zoom, environment and thermal emitter inputs, capture-box
// projection for the HUD), vehicleModels (catalog shapes of the drone layer) and lidarScan (MID-360 scan points). D2.1
// (WP-11b): viewport.requestFrame / setWakeOnInput (power save and static browsing), quality (form factor, deviceClass
// mobile, profile), field (the field view renderer: stats, readout-row highlight, governor notice), touch (long-press
// menu) and fpv (drag in the FPV camera for the gimbal, M15-FR-218). Signatures only grow (M15, SK-E2E callers).
import { events, loop, perf, projectEnu, type CameraPose, type GotoState, type GovernorKnob, type MissionDraft, type PickOptions, type PickResult } from '@/engine'
import type { CallHandle, CallStatus } from '@/net/rt'
import { selectionStore } from '@/stores/selection'
import { vp, type PointPickInfo } from './session'
import { clickAt, fpvRadPerPx, pointInfoAt, setClickConsumer, setWakeOnInput, type ClickConsumer, type FpvDrag, type LongPress } from './interaction'
import { fieldLayer } from './layers/field'
import { currentFormFactor, onFormFactorChange, type FormFactor, type QualityProfile } from './quality'
import type { FieldStats } from '@/engine/field'
import { sendGoto, gotoResults, gotoTargetFor, primaryAgentNo, sendVehicleCommand, vehicleCmdState, type GotoOptions, type GotoRequest, type VehicleCmd, type VehicleCmdState } from './gotoRule'
import { getLayer, pointCloudServices, registerLayer, type LayerId, type LayerSpec } from './layers/registry'
import { requestRebuild } from './hostRuntime'
import { setRedOverride } from './bindings/redOwner'
import { agentNoOf } from './bindings/selection'
import { setLabelFormatter, type LabelFormatter } from './overlay/labelFormatter'
import { viewCube as viewCubeApi } from './overlay/ViewCube'
import { skyControl } from './layers/groundSky'
import { Vector3 } from 'three'
import { registerVehicleModel, type FrameType, type SensorsApi } from '@/engine'
import {
  availableNow, cycleSensorView, projectBox, selectSensor, sensorViewActive, sensorViewInfo, setDigitalZoom, setEmitterSource, setPalette, setSensorEnv,
  setSensorView, sv, type EmitterSource, type LwirPalette, type SensorEnv, type SensorViewInfo, type SensorViewMode, type SensorViewResult,
} from './sensorView'

export type { GotoOptions, GotoRoute, VehicleCmd, VehicleCmdState } from './gotoRule'
export type { CameraPose, MissionDraft } from '@/engine'
export type { LabelFormatter } from './overlay/labelFormatter'
export type { ClickConsumer, FpvDrag, LongPress } from './interaction'
export type { FormFactor, QualityProfile } from './quality'
export type { FieldStats } from '@/engine/field'
export type { EmitterSource, LwirPalette, SensorEnv, SensorViewInfo, SensorViewMode, SensorViewResult } from './sensorView'
export { EMITTER, EMITTER_STRIDE } from './sensorView'

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
  get suspended(): boolean {
    return loop.suspended
  },
  /** D2.1: one frame while suspended (theme change, the first paint of the power-save overlay); a resize does it itself */
  requestFrame(): void {
    loop.requestFrame()
  },
  /** D2.1 static browsing (viewer queue): input on the canvas renders frames for 2 s while suspended (AWR-04 §15.7.7) */
  setWakeOnInput(on: boolean): void {
    setWakeOnInput(on)
  },
  /** writes CSS px (origin top-left of the viewport) into out; false when the point is outside the frustum */
  projectToScreen(enu: ArrayLike<number>, out: Float32Array): boolean {
    const cam = vp.camera
    if (!cam || vp.cssW <= 0) return false
    return projectEnu(cam, vp.cssW, vp.cssH, enu, out, tmp)
  },
  /** ENU ray (origin, unit direction) through a CSS pixel of the viewport; false before the camera exists (M15 editor drag) */
  screenRay(cssX: number, cssY: number, o: Float64Array, d: Float64Array): boolean {
    const cam = vp.camera
    const pk = vp.picker
    if (!cam || !pk || vp.cssW <= 0) return false
    pk.rayAt(cssX, cssY, cam, vp.cssW, vp.cssH)
    o.set(pk.origin)
    d.set(pk.dir)
    return true
  },
  /** terrain height (DTM) at an ENU point from the loaded world (M05 dtm, else the world ground level); 0 before a world */
  groundZ(x: number, y: number): number {
    const dtm = pointCloudServices()?.dtm
    return vp.groundAt(x, y, dtm ? (a, b) => dtm.sample(a, b) : null)
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
  /** route our clicks and double clicks to a consumer first (M15 editor tools); null restores the default click path */
  setClickConsumer(fn: ClickConsumer | null): void {
    setClickConsumer(fn)
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
  /** the route and area editor's draft drawn by the mission overlay (M15 mission-edit; null when the editor closes) */
  setDraft(d: MissionDraft | null): void {
    vp.mission?.setDraft(d)
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

/**
 * D2 sensor views of the FPV camera (AWR-04 §10.5; AWR-14 §20.10.5, URL `sv`): visual approximations only (ADR-108), local
 * display state that never writes to the simulation (the viewer uses them freely, D2-AC-37). The night view reads the
 * illuminators of uav/{id}/payload (M13); the LWIR view the environment (setEnv, from M07 or M15) and the emitter sources
 * (M18 targets; the other vehicles are added by the viewport).
 */
export const sensorView = {
  /** switch the view of the FPV camera; enterFpv first switches the camera to FPV on the focus vehicle */
  set(mode: SensorViewMode, o?: { enterFpv?: boolean; sensorNo?: number }): SensorViewResult {
    return setSensorView(mode, o)
  },
  get mode(): SensorViewMode {
    return sv.mode
  },
  /** a sensor view (not FPV) is drawn this frame */
  get active(): boolean {
    return sensorViewActive()
  },
  /** B key: FPV -> EO -> night -> LWIR over the views the focus vehicle's sensors allow */
  cycle(): SensorViewMode {
    return cycleSensorView()
  },
  /** views available on the FPV focus vehicle (camera sensor: FPV, EO, night; thermal sensor: LWIR) */
  available(): Record<SensorViewMode, boolean> {
    return availableNow()
  },
  /** look through an imaging sensor of the focus vehicle (a thermal one selects LWIR) */
  selectSensor(sensorNo: number): SensorViewResult {
    return selectSensor(sensorNo)
  },
  setPalette(p: LwirPalette): void {
    setPalette(p)
  },
  get palette(): LwirPalette {
    return sv.palette
  },
  /** LWIR digital zoom 1-4 (display only) */
  setDigitalZoom(z: number): void {
    setDigitalZoom(z)
  },
  /** environment inputs: illuminance, air / surface / sky temperatures, emissivity, LWIR extinction, sun direction */
  setEnv(e: Partial<SensorEnv>): void {
    setSensorEnv(e)
  },
  /** thermal emitter source (M18 targets); null removes; returns the unregister function */
  setEmitterSource(id: string, fn: EmitterSource | null): () => void {
    return setEmitterSource(id, fn)
  },
  /** view, sensor (name, kind, focal length, FOV), palette, digital zoom, available views */
  info(): SensorViewInfo {
    return sensorViewInfo()
  },
  /** HUD capture box: CSS px rectangle of a target's yawed box (ENU centre, l w h, yaw) in the current camera */
  projectBox(center: ArrayLike<number>, sizeLwh: ArrayLike<number>, yawRad: number, out: Float32Array): boolean {
    return projectBox(center, sizeLwh, yawRad, out)
  },
  onChange(cb: () => void): () => void {
    return sv.subscribe(cb)
  },
}

/** D2 vehicle catalog shapes (M21 catalog: frame.type, geometry.size_m, ir.motor_delta_t_k) for the drone layer */
export const vehicleModels = {
  register(model: string, info: { frame: FrameType; sizeM: readonly [number, number, number]; motorDeltaTK?: number }): void {
    registerVehicleModel(model, info)
  },
}

/**
 * D2 MID-360 scan display (AWR-14 §20.6.5 "扫描显示"; M13 PRD §6.5.12): the caller subscribes uav/{id}/sensor/lidar/scan
 * (one vehicle per session) and hands every LidarScan8 message to ingest; the points fade out over the persistence
 */
export const lidarScan = {
  ingest(bytes: ArrayBufferView): number {
    return vp.drones?.layer.scan.ingest(bytes, performance.now() / 1000) ?? 0
  },
  /** fade time 0.1-2 s (the "余晖" slider; default 0.6 s) */
  setPersist(s: number): void {
    vp.drones?.layer.scan.setPersist(s)
  },
  clear(): void {
    vp.drones?.layer.scan.clear()
  },
  stats(): { frames: number; written: number; visible: boolean; persistS: number } | null {
    const s = vp.drones?.layer.scan
    return s ? { frames: s.frames, written: s.written, visible: s.points.visible, persistS: s.persistS } : null
  },
}

/** D2.1 form factor and quality profile (AWR-04 §15.8.1, §15.8.6; viewport/quality.ts) */
export const quality = {
  /** live form factor (M15 <html data-ff>, else the window size rule) */
  get formFactor(): FormFactor {
    return currentFormFactor()
  },
  /** the profile fixed at backend creation (frame rate, pixel limit, point budget, caps, rates); null before the backend */
  get profile(): QualityProfile | null {
    return vp.be?.quality ?? null
  },
  /** deviceClass = mobile: a mobile GPU family on a coarse pointer */
  get mobile(): boolean {
    return vp.be?.quality.mobile ?? false
  },
  onFormFactorChange(cb: (ff: FormFactor) => void): () => void {
    return onFormFactorChange(cb)
  },
}

/**
 * D2.1 field view (AWR-04 §15.11; UX-FR-159, FR-160): the view, the volumes and the physical reference are set through the
 * perception store (perceptionActions, M19), the renderer follows engine/perception fieldModel
 */
export const field = {
  /** counts of the last rebuild: anchors, points, points in the solid shell, circles, segments; null when not mounted */
  stats(): FieldStats | null {
    return fieldLayer.renderer?.stats ?? null
  },
  /** readout-row click: the solid shell of this vehicle in the top grey (null clears) */
  highlight(uavId: string | null): void {
    const r = fieldLayer.renderer
    if (r) r.highlight = uavId
  },
  /** the PerfGovernor switched the field view off (M15: "已为保持流畅关闭场视图") */
  onGovernorOff(cb: () => void): () => void {
    return events.on('field.governorOff', () => cb())
  },
}

/** D2.1 touch (AWR-04 §15.8.4): the long press on the canvas (500 ms, < 10 px) for M15's anchored context menu */
export const touch = {
  onLongPress(cb: (p: LongPress) => void): () => void {
    return events.on<LongPress>('viewport.longpress', cb)
  },
}

/**
 * D2 FPV drag (M15-FR-218; request d2-WP-12b-to-M06 item 5): in the FPV camera, a primary-button drag past the click
 * tolerance or a one-finger touch drag on the canvas. dxRad, dyRad: the angles the pointer moved through in the FPV image
 * since the previous event (x right, y down; 'start' since the press, 'end' zeros). The caller throttles the gimbal
 * commands (5 Hz) and keeps the pose after release; picking, selection and the camera are unaffected in FPV.
 */
export const fpv = {
  onDrag(cb: (d: FpvDrag) => void): () => void {
    return events.on<FpvDrag>('fpv.drag', cb)
  },
  /** CSS px to radians of the FPV image at its centre, [x, y]; [0, 0] outside the FPV camera */
  radPerPx(): [number, number] {
    return fpvRadPerPx([0, 0])
  },
}
