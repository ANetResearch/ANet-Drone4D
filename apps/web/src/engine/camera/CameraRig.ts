// CameraRig (M06 §6.11, FR-049..059; AWR-14 §6.4, §6.5; r14 §3.10; r15 §3.15; ADR-046). Owner: M06.
// camera-controls 3.1.2 driven imperatively in the camera phase (controls.update(dtS), never the drei component, so the
// camera is final before the world phase selects points and lays out labels). The controller works in the three frame
// (Y up); every public pose is world ENU and converted with ENU (E, N, U) <-> three (E, U, -N) (ADR-002).
// Modes: orbit (1), free (2), third (3, chase in the velocity frame, focus at tFocus), fpv (4, camera = WorldRoot x
// pose x T_base_cam with the M13 projection, controls disabled), bird (5, north-up top view, pan and zoom only), plus
// followLock (orbit/bird target locked on the focus vehicle, released by a user pan). Mode changes and view tools fly
// with T = clamp(0.4 + 0.15 ln(1 + d/20), 0.4, 1.2) s on --ease-smooth-out, interrupted by any user input; reduced and
// off motion tiers cut. Ground clamp: outside FPV the eye stays >= DTM + 2 m (not during flight60). Unobscured rect:
// the projection centre tweens to the rect centre via setViewOffset (not in FPV, which keeps the sensor principal point).
import CameraControls from 'camera-controls'
import { Box3, Matrix4, PerspectiveCamera, Quaternion, Raycaster, Sphere, Spherical, Vector2, Vector3, Vector4 } from 'three'
import { EASE, MOTION } from '@/lib/tokens/motion.gen'
import { Tween } from '../anim/tween'
import type { FrameCtx } from '../loop'
import { CameraFlight, type FlightReason } from './flight'
import type { SensorsApi, SensorView } from '../drones/frustums'
import { hypot2, hypot3 } from '../hypot'

let installed = false
function install(): void {
  if (installed) return
  // Node (unit tests, M06-NFR-014): camera-controls allocates a DOMRect for its element rect
  const g = globalThis as unknown as { DOMRect?: unknown }
  if (typeof g.DOMRect === 'undefined') {
    g.DOMRect = class {
      constructor(public x = 0, public y = 0, public width = 0, public height = 0) {}
      get left(): number { return this.x }
      get top(): number { return this.y }
      get right(): number { return this.x + this.width }
      get bottom(): number { return this.y + this.height }
    }
  }
  CameraControls.install({ THREE: { Vector2, Vector3, Vector4, Quaternion, Matrix4, Spherical, Box3, Sphere, Raycaster } })
  installed = true
}

export type CameraMode = 'orbit' | 'free' | 'third' | 'fpv' | 'bird'
export interface CameraPose { mode: CameraMode; eye_enu_m: [number, number, number]; target_enu_m: [number, number, number]; fov_deg: number }
export interface SetModeResult { ok: boolean; reason?: 'no_focus' | 'no_camera_sensor' }

export const CAMERA = {
  fovDeg: 60, near: 0.5, nearFpv: 0.2, far: 20_000,
  orbit: { smoothTime: 0.25, draggingSmoothTime: 0.08, minDistance: 2, maxDistance: 5000, maxPolarAngle: 0.49 * Math.PI },
  free: { dist: 0.01, rotateSpeed: -0.3, speedMin: 5, speedMax: 200, aglFactor: 0.5, shift: 4, wheelStep: 1.25, mulMin: 0.25, mulMax: 8 },
  third: { minDistance: 5, maxDistance: 200, back: 15, up: 6, speedRef: 0.5, headingTauS: 0.3, maxYawRate: 0.95 * Math.PI },
  bird: { minDistance: 50, maxDistance: 5000, height: 400 },
  fpvExitM: 60,
  groundClearM: 2,
  focusRadiusM: 60,
  focusPadding: 0.1,
  focusDistanceM: 60,
} as const

/** world ENU -> three frame (E, U, -N) */
export function enuToThree(e: number, n: number, u: number, out: Vector3): Vector3 {
  return out.set(e, u, -n)
}
/** three frame -> world ENU */
export function threeToEnu(v: Vector3, out: Float64Array): Float64Array {
  out[0] = v.x
  out[1] = -v.z
  out[2] = v.y
  return out
}
/** WorldRoot matrix (rotation.x = -pi/2): ENU -> three */
export const WORLD_ROOT = new Matrix4().makeRotationX(-Math.PI / 2)

export interface CameraDeps {
  /** focus vehicle pose at tFocus: position (3), quaternion [x, y, z, w] (4), velocity (3), ENU */
  focusPose?: (agentNo: number, pos: Float64Array, quat: Float64Array, vel: Float64Array) => boolean
  sensors?: () => SensorsApi | null
  /** ground height for the clamp (M05 dtm.sample; coordinate ground.zM before it loads) */
  dtm?: (x: number, y: number) => number
  motionTier?: () => 'full' | 'lite' | 'reduced' | 'off'
  /** M12 interp.setFocus(agentNo) (-1 to leave) */
  setFocus?: (agentNo: number) => void
  /** M05 destination prefetch (camera flight start) */
  prefetch?: (eye: Float64Array, target: Float64Array, fovYRad: number) => void
  onMode?: (mode: CameraMode, followLock: boolean) => void
  onMoved?: (active: boolean) => void
  onFlight?: (phase: 'start' | 'end' | 'cancel', reason: FlightReason) => void
}

const wrapPi = (a: number): number => Math.atan2(Math.sin(a), Math.cos(a))

export class CameraRig {
  readonly controls: CameraControls
  mode: CameraMode = 'orbit'
  followLock = false
  /** focus vehicle (agentNo) of third, fpv and followLock; -1 none */
  focusAgent = -1
  moving = false
  /** flight60 or another external driver owns the camera */
  driven = false
  focusHold = false
  home: { position: [number, number, number]; target: [number, number, number]; fovDeg: number } | null = null
  readonly flight = new CameraFlight()
  private readonly tmpA = new Vector3()
  private readonly tmpB = new Vector3()
  private readonly enuA = new Float64Array(3)
  private readonly pose6 = new Float64Array(6)
  private readonly fPos = new Float64Array(3)
  private readonly fQuat = new Float64Array(4)
  private readonly fVel = new Float64Array(3)
  private readonly m16 = new Float64Array(16)
  private readonly mA = new Matrix4()
  private readonly mB = new Matrix4()
  private readonly q = new Quaternion()
  private readonly one = new Vector3(1, 1, 1)
  private readonly ocx = new Tween()
  private readonly ocy = new Tween()
  private offsetOn = false
  private offW = 0
  private offH = 0
  private psiSmooth = Number.NaN
  private psiApplied = Number.NaN
  private fpvSensor: SensorView | null = null
  private readonly keys = new Set<string>()
  private shift = false
  freeSpeedMul = 1
  private wasMoving = false
  private readonly offs: (() => void)[] = []

  constructor(readonly camera: PerspectiveCamera, dom: HTMLElement | null, private readonly deps: CameraDeps = {}) {
    install()
    camera.fov = CAMERA.fovDeg
    camera.near = CAMERA.near
    camera.far = CAMERA.far
    camera.updateProjectionMatrix()
    this.controls = dom ? new CameraControls(camera, dom) : new CameraControls(camera)
    this.applyModeConfig('orbit')
    const c = this.controls
    const cancel = (): void => this.userInput()
    c.addEventListener('controlstart', cancel)
    this.offs.push(() => c.removeEventListener('controlstart', cancel))
    if (dom) {
      const onWheel = (e: WheelEvent): void => {
        this.userInput()
        if (this.mode === 'free') {
          this.freeSpeedMul = Math.min(CAMERA.free.mulMax, Math.max(CAMERA.free.mulMin, this.freeSpeedMul * (e.deltaY < 0 ? CAMERA.free.wheelStep : 1 / CAMERA.free.wheelStep)))
        }
      }
      dom.addEventListener('wheel', onWheel, { passive: true })
      this.offs.push(() => dom.removeEventListener('wheel', onWheel))
      const kd = (e: KeyboardEvent): void => this.onKey(e, true)
      const ku = (e: KeyboardEvent): void => this.onKey(e, false)
      const blur = (): void => this.keys.clear()
      window.addEventListener('keydown', kd)
      window.addEventListener('keyup', ku)
      window.addEventListener('blur', blur)
      this.offs.push(() => {
        window.removeEventListener('keydown', kd)
        window.removeEventListener('keyup', ku)
        window.removeEventListener('blur', blur)
      })
    }
  }

  // ------------------------------------------------------------------ configuration per mode
  private applyModeConfig(m: CameraMode): void {
    const c = this.controls
    const A = CameraControls.ACTION
    c.enabled = m !== 'fpv' && !this.driven
    c.smoothTime = CAMERA.orbit.smoothTime
    c.draggingSmoothTime = CAMERA.orbit.draggingSmoothTime
    c.dollyToCursor = m === 'orbit'
    c.infinityDolly = false
    c.azimuthRotateSpeed = 1
    c.polarRotateSpeed = 1
    c.minPolarAngle = 0
    c.maxPolarAngle = CAMERA.orbit.maxPolarAngle
    c.minAzimuthAngle = -Infinity
    c.maxAzimuthAngle = Infinity
    c.minDistance = CAMERA.orbit.minDistance
    c.maxDistance = CAMERA.orbit.maxDistance
    c.mouseButtons.left = A.ROTATE
    c.mouseButtons.right = A.TRUCK
    c.mouseButtons.middle = A.DOLLY
    c.mouseButtons.wheel = A.DOLLY
    if (m === 'free') {
      c.minDistance = CAMERA.free.dist
      c.maxDistance = CAMERA.free.dist
      c.azimuthRotateSpeed = CAMERA.free.rotateSpeed
      c.polarRotateSpeed = CAMERA.free.rotateSpeed
      c.maxPolarAngle = Math.PI
      c.mouseButtons.right = A.NONE
      c.mouseButtons.middle = A.NONE
      c.mouseButtons.wheel = A.NONE
    } else if (m === 'third') {
      c.minDistance = CAMERA.third.minDistance
      c.maxDistance = CAMERA.third.maxDistance
      c.mouseButtons.right = A.NONE
    } else if (m === 'bird') {
      c.minPolarAngle = 0
      c.maxPolarAngle = 0
      c.minAzimuthAngle = 0
      c.maxAzimuthAngle = 0
      c.minDistance = CAMERA.bird.minDistance
      c.maxDistance = CAMERA.bird.maxDistance
      c.mouseButtons.left = A.TRUCK
      c.mouseButtons.right = A.NONE
    }
  }

  private emitMode(): void {
    this.deps.onMode?.(this.mode, this.followLock)
  }

  private get reduced(): boolean {
    const t = this.deps.motionTier?.() ?? 'full'
    return t === 'reduced' || t === 'off'
  }

  /** any user input cancels a flight (and a pan releases the follow lock) */
  private userInput(): void {
    if (this.flight.active) {
      this.flight.cancel()
      this.deps.onFlight?.('cancel', this.flight.reason)
    }
    const A = CameraControls.ACTION
    const act = this.controls.currentAction
    if (this.followLock && (act & (A.TRUCK | A.SCREEN_PAN | A.OFFSET)) !== 0) {
      this.followLock = false
      this.deps.setFocus?.(-1)
      this.emitMode()
    }
  }

  private onKey(e: KeyboardEvent, down: boolean): void {
    const t = e.target as HTMLElement | null
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
    this.shift = e.shiftKey
    if (!/^Key[WASDQE]$/.test(e.code)) return
    if (down) {
      if (this.mode !== 'free') return
      this.keys.add(e.code)
      this.userInput()
    } else this.keys.delete(e.code)
  }

  // ------------------------------------------------------------------ pose helpers (ENU)
  /** current eye and target (ENU) into out[0..5] */
  currentPose(out: Float64Array): Float64Array {
    if (this.mode === 'fpv') {
      this.tmpA.setFromMatrixPosition(this.camera.matrixWorld)
      threeToEnu(this.tmpA, this.enuA)
      out[0] = this.enuA[0]
      out[1] = this.enuA[1]
      out[2] = this.enuA[2]
      this.tmpB.set(0, 0, -1).transformDirection(this.camera.matrixWorld).multiplyScalar(10).add(this.tmpA)
      threeToEnu(this.tmpB, this.enuA)
      out[3] = this.enuA[0]
      out[4] = this.enuA[1]
      out[5] = this.enuA[2]
      return out
    }
    this.controls.getPosition(this.tmpA, false)
    this.controls.getTarget(this.tmpB, false)
    out[0] = this.tmpA.x
    out[1] = -this.tmpA.z
    out[2] = this.tmpA.y
    out[3] = this.tmpB.x
    out[4] = -this.tmpB.z
    out[5] = this.tmpB.y
    return out
  }

  /** set eye and target (ENU) immediately */
  lookAtEnu(eye: ArrayLike<number>, target: ArrayLike<number>, transition = false): void {
    enuToThree(eye[0], eye[1], eye[2], this.tmpA)
    enuToThree(target[0], target[1], target[2], this.tmpB)
    void this.controls.setLookAt(this.tmpA.x, this.tmpA.y, this.tmpA.z, this.tmpB.x, this.tmpB.y, this.tmpB.z, transition)
  }

  /** fly to eye and target (ENU); returns the duration in ms (0 for a cut) */
  flyTo(eye: ArrayLike<number>, target: ArrayLike<number>, reason: FlightReason, nowMs = performance.now(), dest: ((b: Float64Array) => boolean) | null = null): number {
    const to = this.pose6
    const from = this.currentPose(new Float64Array(6))
    for (let i = 0; i < 3; i++) {
      to[i] = eye[i]
      to[3 + i] = target[i]
    }
    this.deps.prefetch?.(Float64Array.of(eye[0], eye[1], eye[2]), Float64Array.of(target[0], target[1], target[2]), (this.camera.fov * Math.PI) / 180)
    if (this.reduced) {
      this.flight.cancel()
      this.lookAtEnu(eye, target)
      return 0
    }
    const ms = this.flight.start(from, to, nowMs, reason, dest)
    this.deps.onFlight?.('start', reason)
    return ms
  }

  // ------------------------------------------------------------------ modes
  /** guards: third and fpv need a focus vehicle, fpv a camera sensor (M13 hasCamera) */
  setMode(m: CameraMode, focusAgent: number = this.focusAgent, nowMs = performance.now()): SetModeResult {
    if ((m === 'third' || m === 'fpv') && focusAgent < 0) return { ok: false, reason: 'no_focus' }
    if (m === 'fpv') {
      const api = this.deps.sensors?.() ?? null
      if (!api || !api.hasCamera(focusAgent)) return { ok: false, reason: 'no_camera_sensor' }
      this.fpvSensor = api.sensorsOf(focusAgent).find((s) => s.kind === 0) ?? null
      if (!this.fpvSensor) return { ok: false, reason: 'no_camera_sensor' }
    }
    const prev = this.mode
    const cur = this.currentPose(new Float64Array(6))
    if (prev === 'fpv' && m !== 'fpv') this.leaveFpv()
    this.focusAgent = focusAgent
    const needsFocus = m === 'third' || m === 'fpv'
    // the focus exception of ADR-046 holds in third and fpv and under the follow lock of orbit and bird
    const keepsLock = this.followLock && (m === 'orbit' || m === 'bird')
    if (needsFocus || keepsLock) this.deps.setFocus?.(focusAgent)
    else if (prev === 'third' || prev === 'fpv' || this.followLock) this.deps.setFocus?.(-1)
    if (m !== 'orbit' && m !== 'bird') this.followLock = false
    this.mode = m
    this.applyModeConfig(m)
    this.psiSmooth = Number.NaN
    this.psiApplied = Number.NaN
    // destination pose of the new mode
    const eye = Float64Array.of(cur[0], cur[1], cur[2])
    const tgt = Float64Array.of(cur[3], cur[4], cur[5])
    if (m === 'free') {
      // look from the current eye along the current direction (tiny orbit radius)
      const dx = tgt[0] - eye[0], dy = tgt[1] - eye[1], dz = tgt[2] - eye[2]
      const l = hypot3(dx, dy, dz) || 1
      this.lookAtEnu(eye, [eye[0] + (dx / l) * CAMERA.free.dist, eye[1] + (dy / l) * CAMERA.free.dist, eye[2] + (dz / l) * CAMERA.free.dist])
    } else if (m === 'bird') {
      const h = Math.max(CAMERA.bird.minDistance, Math.min(CAMERA.bird.maxDistance, CAMERA.bird.height))
      this.flyTo([tgt[0], tgt[1] - 1e-3, tgt[2] + h], tgt, 'mode', nowMs)
    } else if (m === 'orbit' && prev === 'fpv') {
      if (this.deps.focusPose?.(focusAgent, this.fPos, this.fQuat, this.fVel)) {
        const psi = this.headingOf(this.fQuat, this.fVel)
        const back = CAMERA.fpvExitM / Math.SQRT2
        this.flyTo([this.fPos[0] - back * Math.cos(psi), this.fPos[1] - back * Math.sin(psi), this.fPos[2] + back], this.fPos, 'mode', nowMs)
      }
    } else if (m === 'third') {
      this.flyTo(this.chaseEye(), this.fPos, 'mode', nowMs, (b) => {
        const e = this.chaseEye()
        for (let i = 0; i < 3; i++) {
          b[i] = e[i]
          b[3 + i] = this.fPos[i]
        }
        return true
      })
    } else if (m === 'fpv') {
      this.flight.cancel()
    }
    this.emitMode()
    return { ok: true }
  }

  private readonly chase = new Float64Array(3)
  /** chase eye behind and above the focus vehicle in its velocity frame (-15, 0, 6) m */
  private chaseEye(): Float64Array {
    if (this.deps.focusPose?.(this.focusAgent, this.fPos, this.fQuat, this.fVel)) {
      const psi = this.headingOf(this.fQuat, this.fVel)
      this.chase[0] = this.fPos[0] - CAMERA.third.back * Math.cos(psi)
      this.chase[1] = this.fPos[1] - CAMERA.third.back * Math.sin(psi)
      this.chase[2] = this.fPos[2] + CAMERA.third.up
    }
    return this.chase
  }

  /** reference heading: horizontal velocity direction, body yaw below 0.5 m/s (r15 §3.15) */
  headingOf(q: Float64Array, v: Float64Array): number {
    if (hypot2(v[0], v[1]) >= CAMERA.third.speedRef) return Math.atan2(v[1], v[0])
    // body x axis rotated by q (x, y, z, w)
    const x = q[0], y = q[1], z = q[2], w = q[3]
    const fx = 1 - 2 * (y * y + z * z)
    const fy = 2 * (x * y + w * z)
    return Math.atan2(fy, fx)
  }

  setFollowLock(on: boolean, focusAgent: number = this.focusAgent): boolean {
    if (this.mode !== 'orbit' && this.mode !== 'bird') return false
    if (on && focusAgent < 0) return false
    this.followLock = on
    if (on) this.focusAgent = focusAgent
    // ADR-046 focus exception: the follow lock uses D_focus like third and fpv (FX2-R3; it was never switched on, so the
    // followed vehicle stayed at tRender = simNow - D_global and t_sim to pixel was the swarm delay, D1-AC-26)
    this.deps.setFocus?.(on ? this.focusAgent : -1)
    this.emitMode()
    return true
  }

  private leaveFpv(): void {
    const cam = this.camera
    cam.matrixAutoUpdate = true
    cam.near = CAMERA.near
    cam.updateProjectionMatrix()
    // sync the controller to the current camera pose
    this.tmpA.setFromMatrixPosition(cam.matrixWorld)
    this.tmpB.set(0, 0, -1).transformDirection(cam.matrixWorld).multiplyScalar(10).add(this.tmpA)
    void this.controls.setLookAt(this.tmpA.x, this.tmpA.y, this.tmpA.z, this.tmpB.x, this.tmpB.y, this.tmpB.z, false)
    this.fpvSensor = null
  }

  /** the focus vehicle was removed or crashed: back to orbit at its last position */
  focusLost(nowMs = performance.now()): void {
    if (this.mode === 'third' || this.mode === 'fpv') {
      this.setMode('orbit', -1, nowMs)
      this.deps.setFocus?.(-1)
    }
    this.focusAgent = -1
    if (this.followLock) {
      this.followLock = false
      this.deps.setFocus?.(-1)
      this.emitMode()
    }
  }

  // ------------------------------------------------------------------ view tools
  /** world home pose (world.json camera.home); transition false cuts (world open), true flies */
  goHome(transition = true, nowMs = performance.now()): void {
    const h = this.home
    if (!h) return
    if (this.mode === 'fpv' || this.mode === 'third') this.setMode('orbit', -1, nowMs)
    if (Math.abs(h.fovDeg - this.camera.fov) > 1e-3) {
      this.camera.fov = h.fovDeg
      this.camera.updateProjectionMatrix()
    }
    if (transition) this.flyTo(h.position, h.target, 'home', nowMs)
    else {
      this.flight.cancel()
      this.lookAtEnu(h.position, h.target)
    }
  }

  /** fit a sphere (ENU centre, radius m) in view, padding 0.1, keeping the direction */
  focusSphere(c: ArrayLike<number>, r: number, nowMs = performance.now()): void {
    const cur = this.currentPose(new Float64Array(6))
    let dx = cur[0] - cur[3], dy = cur[1] - cur[4], dz = cur[2] - cur[5]
    const l = hypot3(dx, dy, dz) || 1
    dx /= l
    dy /= l
    dz /= l
    const half = (Math.min(this.camera.fov, this.camera.fov * this.camera.aspect) * Math.PI) / 360
    const d = (r / Math.sin(half)) * (1 + CAMERA.focusPadding)
    this.flyTo([c[0] + dx * d, c[1] + dy * d, c[2] + dz * d], [c[0], c[1], c[2]], 'focus', nowMs)
  }

  northUp(nowMs = performance.now()): void {
    if (this.mode === 'fpv') return
    const cur = this.currentPose(new Float64Array(6))
    const dh = hypot2(cur[0] - cur[3], cur[1] - cur[4])
    // eye south of the target: screen top = +N
    this.flyTo([cur[3], cur[4] - Math.max(dh, 1e-3), cur[2]], [cur[3], cur[4], cur[5]], 'north', nowMs)
  }

  /** ViewCube: view direction n (ENU unit vector from the target to the eye), target and distance kept */
  viewFrom(n: ArrayLike<number>, nowMs = performance.now()): void {
    if (this.mode === 'fpv') this.setMode('orbit', this.focusAgent, nowMs)
    const cur = this.currentPose(new Float64Array(6))
    const d = hypot3(cur[0] - cur[3], cur[1] - cur[4], cur[2] - cur[5])
    const l = hypot3(n[0], n[1], n[2]) || 1
    let nx = n[0] / l, ny = n[1] / l
    const nz = n[2] / l
    if (hypot2(nx, ny) < 1e-4) ny = -1e-4 // straight down: north up
    if (nz < -0.999) nx = 0
    this.flyTo([cur[3] + nx * d, cur[4] + ny * d, cur[5] + nz * d], [cur[3], cur[4], cur[5]], 'viewcube', nowMs)
  }

  /** double-click on the ground: orbit target to the hit, distance kept */
  retarget(p: ArrayLike<number>, nowMs = performance.now()): void {
    if (this.mode !== 'orbit' && this.mode !== 'bird') return
    const cur = this.currentPose(new Float64Array(6))
    this.flyTo([p[0] + cur[0] - cur[3], p[1] + cur[1] - cur[4], p[2] + cur[2] - cur[5]], p, 'dblclick', nowMs)
  }

  getPose(): CameraPose {
    const c = this.currentPose(new Float64Array(6))
    return { mode: this.mode, eye_enu_m: [c[0], c[1], c[2]], target_enu_m: [c[3], c[4], c[5]], fov_deg: this.camera.fov }
  }

  setPose(p: CameraPose, o: { fly?: boolean } = {}, nowMs = performance.now()): void {
    const m = p.mode === 'third' || p.mode === 'fpv' ? 'orbit' : p.mode
    if (m !== this.mode) this.setMode(m, -1, nowMs)
    if (Number.isFinite(p.fov_deg) && Math.abs(p.fov_deg - this.camera.fov) > 1e-3) {
      this.camera.fov = p.fov_deg
      this.camera.updateProjectionMatrix()
    }
    if (o.fly) this.flyTo(p.eye_enu_m, p.target_enu_m, 'pose', nowMs)
    else this.lookAtEnu(p.eye_enu_m, p.target_enu_m)
  }

  /** unobscured rect centre (CSS px) of a W x H canvas; null clears the offset; tweened over durationMs */
  setViewCentre(cx: number | null, cy: number, w: number, h: number, durationMs = 0, nowMs = performance.now()): void {
    if (cx === null || w <= 0 || h <= 0) {
      this.offsetOn = false
      return
    }
    const d = this.reduced ? 0 : durationMs
    if (!this.offsetOn || this.offW !== w || this.offH !== h) {
      this.ocx.jump(this.offsetOn ? this.ocx.value(nowMs) : w / 2)
      this.ocy.jump(this.offsetOn ? this.ocy.value(nowMs) : h / 2)
    }
    this.ocx.start(this.ocx.value(nowMs), cx, nowMs, d, EASE.smoothOut)
    this.ocy.start(this.ocy.value(nowMs), cy, nowMs, d, EASE.smoothOut)
    this.offsetOn = true
    this.offW = w
    this.offH = h
  }

  // ------------------------------------------------------------------ camera phase
  /** external driver (flight60): controller input off, no flights, no ground clamp; false hands the pose back */
  setDriven(on: boolean): void {
    if (this.driven === on) return
    this.driven = on
    this.flight.cancel()
    this.applyModeConfig(this.mode)
    if (!on) {
      const cam = this.camera
      this.tmpB.set(0, 0, -1).applyQuaternion(cam.quaternion).multiplyScalar(100).add(cam.position)
      void this.controls.setLookAt(cam.position.x, cam.position.y, cam.position.z, this.tmpB.x, this.tmpB.y, this.tmpB.z, false)
    }
  }

  /** driven pose (ENU eye and target), applied directly to the camera; update() then adds the view offset */
  drive(eye: ArrayLike<number>, target: ArrayLike<number>): void {
    const cam = this.camera
    enuToThree(eye[0], eye[1], eye[2], cam.position)
    enuToThree(target[0], target[1], target[2], this.tmpB)
    cam.up.set(0, 1, 0)
    cam.lookAt(this.tmpB)
  }

  update(ctx: FrameCtx): void {
    const dtS = Math.min(Math.max(ctx.dtMs, 0), 100) / 1000
    const cam = this.camera
    if (this.driven) {
      this.applyViewOffset(ctx)
      cam.updateMatrixWorld()
      this.moving = true
      return
    }
    if (this.mode === 'fpv') {
      this.updateFpv(ctx)
      this.moving = true
      this.notifyMoving()
      return
    }
    const st = this.flight.step(ctx.nowMs)
    if (st !== 0) {
      const o = this.flight.out
      enuToThree(o[0], o[1], o[2], this.tmpA)
      enuToThree(o[3], o[4], o[5], this.tmpB)
      void this.controls.setLookAt(this.tmpA.x, this.tmpA.y, this.tmpA.z, this.tmpB.x, this.tmpB.y, this.tmpB.z, false)
      if (st === 2) this.deps.onFlight?.('end', this.flight.reason)
    }
    const flying = st === 1
    if (!flying) {
      if (this.mode === 'third') this.updateThird(dtS)
      else if (this.followLock && this.focusAgent >= 0 && this.deps.focusPose?.(this.focusAgent, this.fPos, this.fQuat, this.fVel)) {
        enuToThree(this.fPos[0], this.fPos[1], this.fPos[2], this.tmpA)
        void this.controls.moveTo(this.tmpA.x, this.tmpA.y, this.tmpA.z, false)
      }
      if (this.mode === 'free' && this.keys.size > 0) this.updateFreeKeys(dtS)
    }
    const upd = this.controls.update(dtS)
    this.moving = upd || flying
    this.clampGround()
    this.applyViewOffset(ctx)
    cam.updateMatrixWorld()
    this.notifyMoving()
  }

  private notifyMoving(): void {
    if (this.moving !== this.wasMoving) {
      this.wasMoving = this.moving
      this.deps.onMoved?.(this.moving)
    }
  }

  private updateThird(dtS: number): void {
    if (!this.deps.focusPose?.(this.focusAgent, this.fPos, this.fQuat, this.fVel)) return
    const psi = this.headingOf(this.fQuat, this.fVel)
    if (Number.isNaN(this.psiSmooth)) {
      this.psiSmooth = psi
      this.psiApplied = psi
    } else {
      // first-order smoothing (tau 0.3 s) with a slew limit so a large reference change never spins the view
      // faster than 180 deg/s (M06-AC-040: switching to the body heading at low speed without jumps)
      let step = wrapPi(psi - this.psiSmooth) * (1 - Math.exp(-dtS / CAMERA.third.headingTauS))
      const cap = CAMERA.third.maxYawRate * dtS
      if (step > cap) step = cap
      else if (step < -cap) step = -cap
      this.psiSmooth = wrapPi(this.psiSmooth + step)
    }
    const d = wrapPi(this.psiSmooth - this.psiApplied)
    this.psiApplied = this.psiSmooth
    // azimuth theta = psi - pi/2 + const: the camera follows heading changes one to one
    if (d !== 0) void this.controls.rotate(d, 0, false)
    enuToThree(this.fPos[0], this.fPos[1], this.fPos[2], this.tmpA)
    void this.controls.moveTo(this.tmpA.x, this.tmpA.y, this.tmpA.z, false)
  }

  private updateFreeKeys(dtS: number): void {
    this.controls.getPosition(this.tmpA, false)
    threeToEnu(this.tmpA, this.enuA)
    const agl = this.enuA[2] - (this.deps.dtm?.(this.enuA[0], this.enuA[1]) ?? 0)
    const v = Math.min(CAMERA.free.speedMax, Math.max(CAMERA.free.speedMin, CAMERA.free.aglFactor * agl)) * (this.shift ? CAMERA.free.shift : 1) * this.freeSpeedMul
    const s = v * dtS
    const k = this.keys
    const fwd = (k.has('KeyW') ? 1 : 0) - (k.has('KeyS') ? 1 : 0)
    const side = (k.has('KeyD') ? 1 : 0) - (k.has('KeyA') ? 1 : 0)
    const up = (k.has('KeyE') ? 1 : 0) - (k.has('KeyQ') ? 1 : 0)
    if (fwd) void this.controls.forward(fwd * s, false)
    if (side) void this.controls.truck(side * s, 0, false)
    if (up) void this.controls.elevate(up * s, false)
  }

  private updateFpv(ctx: FrameCtx): void {
    const api = this.deps.sensors?.() ?? null
    const s = this.fpvSensor
    const cam = this.camera
    if (!api || !s || !this.deps.focusPose?.(this.focusAgent, this.fPos, this.fQuat, this.fVel)) return
    cam.matrixAutoUpdate = false
    this.q.set(this.fQuat[0], this.fQuat[1], this.fQuat[2], this.fQuat[3])
    this.tmpA.set(this.fPos[0], this.fPos[1], this.fPos[2])
    this.mA.compose(this.tmpA, this.q, this.one) // body pose in ENU
    this.mB.fromArray(api.T_base_cam(s, this.m16))
    this.mA.premultiply(WORLD_ROOT).multiply(this.mB)
    cam.matrix.copy(this.mA)
    cam.matrixWorld.copy(this.mA)
    cam.matrixWorldInverse.copy(this.mA).invert()
    cam.matrixWorldNeedsUpdate = false
    const aspect = ctx.cssH > 0 ? ctx.cssW / ctx.cssH : cam.aspect
    cam.near = CAMERA.nearFpv
    if (cam.view !== null) cam.clearViewOffset()
    cam.projectionMatrix.fromArray(api.projectionFor(s, aspect, CAMERA.nearFpv, CAMERA.far, this.m16))
    cam.projectionMatrixInverse.copy(cam.projectionMatrix).invert()
    cam.position.setFromMatrixPosition(this.mA)
    cam.quaternion.setFromRotationMatrix(this.mA)
  }

  /** eye >= DTM + 2 m outside FPV (the target follows so the view does not jump) */
  private clampGround(): void {
    const dtm = this.deps.dtm
    if (!dtm) return
    this.controls.getPosition(this.tmpA, false)
    const e = this.tmpA.x
    const n = -this.tmpA.z
    const u = this.tmpA.y
    const floor = dtm(e, n) + CAMERA.groundClearM
    if (!(u < floor)) return
    const d = floor - u
    this.controls.getTarget(this.tmpB, false)
    void this.controls.setLookAt(this.tmpA.x, this.tmpA.y + d, this.tmpA.z, this.tmpB.x, this.tmpB.y + d, this.tmpB.z, false)
    void this.controls.update(0)
  }

  private applyViewOffset(ctx: FrameCtx): void {
    const cam = this.camera
    if (!this.offsetOn || ctx.cssW <= 0 || ctx.cssH <= 0) {
      if (cam.view !== null && cam.view.enabled) cam.clearViewOffset()
      return
    }
    const W = ctx.cssW
    const H = ctx.cssH
    const sx = this.offW > 0 ? W / this.offW : 1
    const sy = this.offH > 0 ? H / this.offH : 1
    const cx = this.ocx.value(ctx.nowMs) * sx
    const cy = this.ocy.value(ctx.nowMs) * sy
    const v = cam.view
    const ox = W / 2 - cx
    const oy = H / 2 - cy
    if (v && v.enabled && v.fullWidth === W && v.fullHeight === H && Math.abs(v.offsetX - ox) < 1e-3 && Math.abs(v.offsetY - oy) < 1e-3) return
    cam.setViewOffset(W, H, ox, oy, W, H)
  }

  dispose(): void {
    for (const off of this.offs) off()
    this.controls.dispose()
  }
}

export const CAMERA_FLIGHT_MS = { min: MOTION.cameraMinMs, max: MOTION.cameraMaxMs }
