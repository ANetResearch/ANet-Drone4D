// Manual control controller (R-D2-21; AWR-04 §10.6; M15-FR-215 to FR-217; UX-FR-132 to UX-FR-135; AWR-14 §20.10.1 to
// §20.10.4, M15 §15.6.1). States IDLE -> CONFIRMING (the operator confirms the takeover) -> ACQUIRING (`acquire`, then
// the streaming `velocity` call with frame body or fw and keepout_guard clamp or warn) -> ACTIVE (manual key scope
// registered, camera to FPV or third person, setpoint pump at 30 Hz, rate locked x1 by the server) -> STOPPING (FINAL
// setpoint, `velocity_stop`, `release`) -> IDLE. Esc, "release", a window blur or a hidden page stop and release; a
// preempted lease, a safety action, the watchdog or a removed vehicle end the call and drop back to IDLE with a notice.
// The pump runs on a timer outside React (no render per setpoint): it merges the keys (first-order smoothed) and the
// virtual sticks, sends VelSetpoint16 through the page client's CLIENT_DATA channel (publishSetpoint), keeps sending
// zeros for ZERO_TAIL packets after the inputs return to zero and then one zero every IDLE_KEEPALIVE_TICKS ticks (D2-FX-R2-web,
// D2-AC-48: the server's 250 ms stream watchdog ended an idle takeover within a quarter second, 209), and every 100 ms predicts keep-out entries 3 s ahead from
// the vehicle's Full64 position and velocity (HUD line and one warning toast per target per 5 s). D2-FX-R2-web (D2-AC-21):
// the prediction also runs with the commanded velocity (the vehicle converges on it within about a second, so a straight
// path at the current velocity warned only 2-3 s ahead while the operator accelerated toward a volume) and looks
// PREDICT_LEAD_S further than the 3 s of the criterion (data age, the 0.1 s prediction step); the clamp state follows the
// server's `uav.keepout_guard{clamped}` events of the controlled vehicle (the soft fence emits no call warning), held for
// CLAMP_HOLD_MS after the last one, as well as a KEEPOUT_CLAMPED progress warning. Interim: the keep-out
// volumes come from the sandbox REST target configurations (ui/sandbox/domain.ts) until M18's stores/targets.ts exposes
// them; the pump runs on the main thread until rt.worker hosts it (request to M11).
import { useStore } from 'zustand'
import { reasonText, t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { createAwrStore } from '@/lib/createStore'
import type { CallHandle, CallResult, RtClient } from '@/net/rt'
import { selection } from '@/stores/selection'
import { camera, type CameraMode } from '@/viewport/facade'
import { modalOpenInDom, registerHotkey } from '@/ui/hotkeys/registry'
import { toolMode } from '@/ui/tools/toolMode'
import { UX } from '@/ui/testing/uxProbe'
import { isSandboxOperator } from '@/app/shell/surface'
import {
  KEY_TAU_S, PREDICT_S, PUMP_HZ, ZERO_INPUT, ZERO_TAIL, bodyToWorld, fwSetpoint, isZero, mcSetpoint, merge, nextTier, predictEntry, smooth,
  type Approach, type FwLimits, type FwState, type KeepVolume, type ManualInput, type McLimits, type SpeedTier,
} from './model'

export type ManualState = 'IDLE' | 'CONFIRMING' | 'ACQUIRING' | 'ACTIVE' | 'STOPPING'
export interface ManualView {
  state: ManualState
  vehicle: string | null
  tier: SpeedTier
  fw: boolean
  /** "keepout_guard": clamp (soft fence on, default) or warn (fence off, entering counts as discovery) */
  guard: 'clamp' | 'warn'
  approach: Approach | null
  clamped: boolean
  version: number
}
export const manualStore = createAwrStore<ManualView>('ui.manual', () => ({
  state: 'IDLE', vehicle: null, tier: 'normal', fw: false, guard: 'clamp', approach: null, clamped: false, version: 0,
}))
export const useManual = <T,>(sel: (s: ManualView) => T): T => useStore(manualStore, sel)
const setM = (p: Partial<ManualView>): void => {
  manualStore.setState({ ...p, version: manualStore.getState().version + 1 })
  const s = manualStore.getState()
  UX.manual.state = s.state
  UX.manual.clamped = s.clamped
  UX.manual.approachS = s.approach ? s.approach.tS : null
}

// ------------------------------------------------------------ inputs (mutable, never in React state)
const keyTarget: ManualInput = { ...ZERO_INPUT }
const keyAxes: ManualInput = { ...ZERO_INPUT }
const stickAxes: ManualInput = { ...ZERO_INPUT }
const merged: ManualInput = { ...ZERO_INPUT }
const sp = new Float32Array(4)
const fwState: FwState = { vCmd: Number.NaN }

/** the virtual sticks write here (normalised, -1..1); the pump reads at 30 Hz */
export function setStick(side: 'left' | 'right', x: number, y: number): void {
  if (side === 'left') {
    stickAxes.up = y
    stickAxes.yawRight = x
  } else {
    stickAxes.fwd = y
    stickAxes.right = x
  }
  UX.manual.stick = [stickAxes.up, stickAxes.yawRight, stickAxes.fwd, stickAxes.right]
}

const KEY_AXES: Readonly<Record<string, [keyof ManualInput, number]>> = {
  KeyW: ['fwd', 1], KeyS: ['fwd', -1], KeyD: ['right', 1], KeyA: ['right', -1], KeyE: ['up', 1], KeyQ: ['up', -1],
  ArrowRight: ['yawRight', 1], ArrowLeft: ['yawRight', -1],
}
const held = new Set<string>()
function recomputeKeys(): void {
  keyTarget.fwd = 0
  keyTarget.right = 0
  keyTarget.up = 0
  keyTarget.yawRight = 0
  for (const code of held) {
    const a = KEY_AXES[code]
    if (a) keyTarget[a[0]] = Math.max(-1, Math.min(1, keyTarget[a[0]] + a[1]))
  }
}

// ------------------------------------------------------------ dependencies (the page client and the domain data)
export interface ManualDeps {
  rt: () => RtClient | null
  /** model limits of the vehicle (multirotor vmax, fixed-wing envelope); null when unknown */
  limits: (vehicle: string) => { fw: boolean; mc: McLimits; fwl: FwLimits | null }
  /** keep-out volumes in force (fallback when `predict` is not given) */
  volumes: () => readonly KeepVolume[]
  /** the domain's entry prediction (M18 manualKeepoutWarning: current criterion bodies of the target model) within the
   *  horizon (s); null: predict from `volumes` */
  predict: ((p: readonly [number, number, number], v: readonly [number, number, number], horizonS: number) => Approach | null) | null
}
const DEFAULT_FW: FwLimits = { vMinMps: 14, vMaxMps: 30, vCruiseMps: 20, bankMaxRad: (30 * Math.PI) / 180, climbMaxMps: 4 }
let deps: ManualDeps = {
  rt: () => null,
  limits: () => ({ fw: false, mc: { vmaxMps: 10 }, fwl: null }),
  volumes: () => [],
  predict: null,
}
export function setManualDeps(d: Partial<ManualDeps>): void {
  deps = { ...deps, ...d }
}

// ------------------------------------------------------------ pump and prediction
let pumpTimer: ReturnType<typeof setInterval> | null = null
let predictTimer: ReturnType<typeof setInterval> | null = null
let zeroLeft = 0
let idleTicks = 0
/** with every input at zero a setpoint goes out every 3rd tick (10 Hz at the 30 Hz pump; the stream watchdog is 250 ms) */
export const IDLE_KEEPALIVE_TICKS = 3
let lastTickMs = 0
let velocityCall: CallHandle | null = null
let prevCamera: CameraMode | null = null
let offScope: (() => void)[] = []

function agentNoOf(rt: RtClient, id: string): number {
  return rt.roster.agentNoOf(id)
}

/** one pump tick (exported for tests): smoothing, merge, mapping, send */
export function pumpTick(nowMs = performance.now()): boolean {
  const s = manualStore.getState()
  const rt = deps.rt()
  if (s.state !== 'ACTIVE' || !s.vehicle || !rt) return false
  UX.manual.ticks++
  const dt = lastTickMs ? Math.min(0.2, (nowMs - lastTickMs) / 1000) : 1 / PUMP_HZ
  lastTickMs = nowMs
  keyAxes.fwd = smooth(keyAxes.fwd, keyTarget.fwd, dt, KEY_TAU_S)
  keyAxes.right = smooth(keyAxes.right, keyTarget.right, dt, KEY_TAU_S)
  keyAxes.up = smooth(keyAxes.up, keyTarget.up, dt, KEY_TAU_S)
  keyAxes.yawRight = smooth(keyAxes.yawRight, keyTarget.yawRight, dt, KEY_TAU_S)
  merge(keyAxes, stickAxes, merged)
  const zero = isZero(merged)
  if (zero) {
    // idle: one zero setpoint every IDLE_KEEPALIVE_TICKS ticks keeps the stream (the server's stream watchdog ends a
    // velocity call after 250 ms without a setpoint, D1 rule, AWR-14 §20.10 table: releasing every input is not a release)
    if (zeroLeft <= 0 && ++idleTicks % IDLE_KEEPALIVE_TICKS !== 0) {
      UX.manual.idle++
      return false
    }
    if (zeroLeft > 0) zeroLeft--
  } else {
    zeroLeft = ZERO_TAIL
    idleTicks = 0
  }
  const lim = deps.limits(s.vehicle)
  if (s.fw) fwSetpoint(merged, s.tier, lim.fwl ?? DEFAULT_FW, fwState, dt, Number.NaN, sp)
  else mcSetpoint(merged, s.tier, lim.mc, sp)
  const no = agentNoOf(rt, s.vehicle)
  if (no < 0) {
    UX.manual.noAgent++
    return false
  }
  rt.publishSetpoint(no, sp[0], sp[1], sp[2], sp[3], false)
  lastSp.set(sp)
  lastSpFw = s.fw
  UX.sandbox.setpoints++
  return true
}

const pos = [0, 0, 0]
const vel = [0, 0, 0]
const cmd = [0, 0, 0]
let yawEnu = 0
/** the last setpoint sent ([fwd or airspeed, left, up, yaw rate]) and whether it was a fixed-wing one; zero when idle */
const lastSp = new Float32Array(4)
let lastSpFw = false
/** the controlled vehicle's position, velocity and heading (ENU) from the swarm snapshot; false when not in it */
function vehicleState(rt: RtClient, id: string): boolean {
  const no = agentNoOf(rt, id)
  const sw = rt.swarm
  for (let i = 0; i < sw.n; i++) {
    if (sw.agentNo[i] !== no) continue
    pos[0] = sw.pos[i * 3]
    pos[1] = sw.pos[i * 3 + 1]
    pos[2] = sw.pos[i * 3 + 2]
    vel[0] = sw.vel[i * 3]
    vel[1] = sw.vel[i * 3 + 1]
    vel[2] = sw.vel[i * 3 + 2]
    const x = sw.quat[i * 4], y = sw.quat[i * 4 + 1], z = sw.quat[i * 4 + 2], w = sw.quat[i * 4 + 3]
    yawEnu = Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return true
  }
  return false
}

/** the commanded velocity in ENU (body FLU or the fixed-wing airspeed along the heading); false when nothing is commanded */
function commandedWorld(out: number[]): boolean {
  const fwd = lastSp[0]
  const left = lastSpFw ? 0 : lastSp[1]
  if (!(Math.abs(fwd) > 1e-3 || Math.abs(left) > 1e-3 || Math.abs(lastSp[2]) > 1e-3)) return false
  const [e, n] = bodyToWorld(fwd, left, yawEnu)
  out[0] = e
  out[1] = n
  out[2] = lastSp[2]
  return true
}

/** the earlier of the predictions with the current and the commanded velocity */
export function earliestApproach(a: Approach | null, b: Approach | null): Approach | null {
  if (!a) return b
  if (!b) return a
  return b.tS < a.tS ? b : a
}

const lastWarn = new Map<string, number>()
export const APPROACH_DEDUPE_MS = 5000
/** look this much further than the 3 s of the criterion (data age, the prediction step, acceleration not modelled) */
export const PREDICT_LEAD_S = 0.5
/** the clamp state stays on this long after the last server clamp event (the guard reports at most once a second) */
export const CLAMP_HOLD_MS = 1600
let lastClampMs = Number.NEGATIVE_INFINITY
let offEvents: (() => void) | null = null
function noteClamp(): void {
  lastClampMs = performance.now()
  if (!manualStore.getState().clamped) setM({ clamped: true })
}
function predictWith(v: readonly number[], horizonS: number): Approach | null {
  return deps.predict ? deps.predict(pos as unknown as readonly [number, number, number], v as unknown as readonly [number, number, number], horizonS)
    : predictEntry(pos, v, deps.volumes(), horizonS)
}
function predictTick(): void {
  const s = manualStore.getState()
  if (s.clamped && performance.now() - lastClampMs > CLAMP_HOLD_MS) setM({ clamped: false })
  const rt = deps.rt()
  if (s.state !== 'ACTIVE' || !s.vehicle || !rt || !vehicleState(rt, s.vehicle)) return
  const horizon = PREDICT_S + PREDICT_LEAD_S
  let a = predictWith(vel, horizon)
  if (commandedWorld(cmd)) a = earliestApproach(a, predictWith(cmd, horizon))
  const prev = s.approach
  if ((a === null) !== (prev === null) || (a && prev && (a.target !== prev.target || Math.abs(a.tS - prev.tS) > 0.25))) setM({ approach: a })
  if (a && a.tS > 0) {
    const now = Date.now()
    const at = lastWarn.get(a.target) ?? 0
    if (now - at >= APPROACH_DEDUPE_MS) {
      lastWarn.set(a.target, now)
      notify(`manual:approach:${a.target}`, 'warning', t('manual.approach', { t: a.tS.toFixed(1), target: a.target, kind: t(`sens.kind.${a.kind}`) }))
    }
  }
}

function startLoops(): void {
  stopLoops()
  lastTickMs = 0
  zeroLeft = 0
  idleTicks = IDLE_KEEPALIVE_TICKS - 1
  lastSp.fill(0)
  pumpTimer = setInterval(() => pumpTick(), Math.round(1000 / PUMP_HZ))
  predictTimer = setInterval(predictTick, 100)
}
function stopLoops(): void {
  if (pumpTimer) clearInterval(pumpTimer)
  if (predictTimer) clearInterval(predictTimer)
  pumpTimer = null
  predictTimer = null
  offEvents?.()
  offEvents = null
  lastClampMs = Number.NEGATIVE_INFINITY
}

/** the soft fence's reports for the controlled vehicle (`uav.keepout_guard`, clamped unless false) */
export function isClampEvent(e: { type: string; uav?: string | null; data?: Record<string, unknown> }, vehicle: string): boolean {
  if (e.type !== 'uav.keepout_guard') return false
  const d = e.data ?? {}
  const uav = typeof d.uav === 'string' ? d.uav : e.uav
  return uav === vehicle && d.clamped !== false
}

// ------------------------------------------------------------ key scope (manual > tool > editor > list/viewport > global)
const BLOCKED = ['KeyF', 'Space', 'KeyG', 'Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5']
/** the manual scope's keys for the shortcut help (AWR-14 §20.10.2) */
export const MANUAL_KEYS: readonly { combo: string; labelKey: string }[] = [
  ...['KeyW', 'KeyS', 'KeyA', 'KeyD', 'KeyQ', 'KeyE', 'ArrowLeft', 'ArrowRight', 'KeyC', 'Escape'].map((c) => ({ combo: c, labelKey: `manual.key.${c}` })),
]
function onKeyUp(e: KeyboardEvent): void {
  if (held.delete(e.code)) recomputeKeys()
}
function onBlur(): void {
  UX.manual.ended = 'blur'
  if (manualStore.getState().state === 'ACTIVE') void manual.release()
}
function onHidden(): void {
  if (document.visibilityState === 'hidden') UX.manual.ended = 'hidden'
  if (document.visibilityState === 'hidden' && manualStore.getState().state === 'ACTIVE') void manual.release()
}

function enterScope(): void {
  leaveScope()
  const active = () => manualStore.getState().state === 'ACTIVE'
  for (const code of Object.keys(KEY_AXES)) {
    offScope.push(registerHotkey({ id: `manual.axis.${code}`, combo: code, scope: 'manual', labelKey: `manual.key.${code}`, group: 'manual', when: active,
      deniedKey: () => null, run: () => {
        held.add(code)
        recomputeKeys()
      } }))
  }
  offScope.push(registerHotkey({ id: 'manual.tier', combo: 'KeyC', scope: 'manual', labelKey: 'manual.key.KeyC', group: 'manual', when: active, deniedKey: () => null,
    run: () => setM({ tier: nextTier(manualStore.getState().tier) }) }))
  // Esc closes the topmost overlay first (its own handler); only without one it leaves manual control
  offScope.push(registerHotkey({ id: 'manual.exit', combo: 'Escape', scope: 'manual', labelKey: 'manual.key.Escape', group: 'manual',
    when: () => active() && !(typeof document !== 'undefined' && modalOpenInDom(document)), deniedKey: () => null, run: () => void manual.release() }))
  for (const code of BLOCKED) {
    offScope.push(registerHotkey({ id: `manual.block.${code}`, combo: code, scope: 'manual', labelKey: 'manual.blocked', group: 'manual', when: active,
      deniedKey: () => null, run: () => toolMode.flash('manual.blocked') }))
  }
  if (typeof window !== 'undefined') {
    window.addEventListener('keyup', onKeyUp, true)
    window.addEventListener('blur', onBlur)
    document.addEventListener('visibilitychange', onHidden)
  }
}
function leaveScope(): void {
  for (const off of offScope) off()
  offScope = []
  held.clear()
  recomputeKeys()
  if (typeof window !== 'undefined') {
    window.removeEventListener('keyup', onKeyUp, true)
    window.removeEventListener('blur', onBlur)
    document.removeEventListener('visibilitychange', onHidden)
  }
}

function toIdle(noticeKey: string | null, code?: number): void {
  UX.manual.ended = `${noticeKey ?? 'release'}${code ? `:${code}` : ''}`
  // a stream the server ended (watchdog, preemption, safety): close the setpoint channel too, so the worker's keepalive
  // (net/rt/session.ts spKeepTick) stops at once
  const cur = manualStore.getState()
  const rt = deps.rt()
  if (rt && cur.vehicle && cur.state === 'ACTIVE') {
    const no = agentNoOf(rt, cur.vehicle)
    if (no >= 0) rt.publishSetpoint(no, 0, 0, 0, 0, true)
  }
  stopLoops()
  leaveScope()
  velocityCall = null
  if (prevCamera) void camera.setMode(prevCamera)
  prevCamera = null
  setM({ state: 'IDLE', vehicle: null, approach: null, clamped: false })
  if (noticeKey) notify('manual:state', 'warning', t(noticeKey), code ? `${code} ${reasonText(code).short}` : undefined)
}

const runningOrDone = (r: CallResult): boolean => r.status === 'running' || r.status === 'accepted' || r.final === true

export const manual = {
  /** "manual control" on a vehicle: ask first (the takeover reassigns the vehicle's work items) */
  request(vehicle: string): boolean {
    if (!isSandboxOperator() || manualStore.getState().state !== 'IDLE') return false
    setM({ state: 'CONFIRMING', vehicle })
    return true
  },
  cancel(): void {
    if (manualStore.getState().state === 'CONFIRMING') setM({ state: 'IDLE', vehicle: null })
  },
  /** confirmed: acquire the lease, then the velocity stream */
  async confirm(): Promise<boolean> {
    const s = manualStore.getState()
    const rt = deps.rt()
    if (s.state !== 'CONFIRMING' || !s.vehicle || !rt) {
      setM({ state: 'IDLE', vehicle: null })
      return false
    }
    const id = s.vehicle
    const lim = deps.limits(id)
    setM({ state: 'ACQUIRING', fw: lim.fw })
    const acq = await rt.call(`uav/${id}/cmd/acquire`, {}).result
    if (acq.status !== 'succeeded') {
      toIdle('manual.refused', acq.code)
      return false
    }
    const call = rt.call(`uav/${id}/cmd/velocity`, { frame: lim.fw ? 'fw' : 'body', hold_alt: false, keepout_guard: s.guard })
    velocityCall = call
    const first = await new Promise<CallResult>((resolve) => call.onResult((r) => {
      if (runningOrDone(r)) resolve(r)
    }))
    if (first.status !== 'running' && first.status !== 'accepted') {
      void rt.call(`uav/${id}/cmd/release`, { return_to: 'none' }).result
      toIdle('manual.refused', first.code)
      return false
    }
    // the stream ended by the server: preemption (210), safety action (204), watchdog (209), vehicle removed
    call.onResult((r) => {
      if (r.final && manualStore.getState().state === 'ACTIVE' && velocityCall === call) toIdle(r.code === 209 ? 'manual.watchdog' : 'manual.ended', r.code)
    })
    call.onProgress((p) => {
      const w = (p as { warnings?: unknown }).warnings
      if (Array.isArray(w) && w.includes('KEEPOUT_CLAMPED')) noteClamp()
    })
    selection.select([id])
    prevCamera = camera.mode
    void camera.setMode('fpv')
    fwState.vCmd = Number.NaN
    setM({ state: 'ACTIVE' })
    enterScope()
    startLoops()
    offEvents = rt.onEvents((b) => {
      for (const e of b) if (isClampEvent(e, id)) noteClamp()
    })
    return true
  },
  /** stop and release (Esc, the release button, blur, hidden page) */
  async release(o: { returnHome?: boolean } = {}): Promise<void> {
    const s = manualStore.getState()
    const rt = deps.rt()
    if (s.state !== 'ACTIVE' || !s.vehicle) return
    const id = s.vehicle
    setM({ state: 'STOPPING' })
    stopLoops()
    if (rt) {
      const no = agentNoOf(rt, id)
      if (no >= 0) rt.publishSetpoint(no, 0, 0, 0, 0, true)
      velocityCall = null
      await rt.call(`uav/${id}/cmd/velocity_stop`, {}).result
      if (o.returnHome) await rt.call(`uav/${id}/cmd/rtl`, {}).result
      await rt.call(`uav/${id}/cmd/release`, { return_to: 'none' }).result
    }
    toIdle(null)
  },
  setTier(tier: SpeedTier): void {
    setM({ tier })
  },
  /** the soft fence switch of the settings (applies to the next takeover) */
  setGuard(guard: 'clamp' | 'warn'): void {
    setM({ guard })
  },
}

/** the clock speed is locked x1 while the vehicle is under manual control (AWR-04 §10.6 item 1) */
export const manualActive = (): boolean => {
  const st = manualStore.getState().state
  return st === 'ACTIVE' || st === 'ACQUIRING'
}
