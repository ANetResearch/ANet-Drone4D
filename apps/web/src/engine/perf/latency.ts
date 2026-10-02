// Latency metrics (M06 §6.16, FR-078; ADR-046; AWR-18 §7.3, §9.3; D1-AC-26). Owner: M06.
//   tSimToPixelMs  focus vehicle (else the selected one): (simNow of this rAF - sim time of the pose presented in the
//                  previous frame) / rate, in wall ms; once per frame
//   cmdToVisibleMs mark('cmd.sent') to the first frame that shows the command's effect: the vehicle's newest flight
//                  state or control owner switched as the command switches it (takeoff to TAKING_OFF, land to LANDING;
//                  hover and goto: the state byte, i.e. the FLYING sub-state HOVER or GOTO, or the control owner to
//                  OPERATOR), different from what was shown
//                  at mark time, and the rendered time (tRender, tFocus) has reached the sample that carried the switch
//                  (FX2-R3: the effect as AWR-18 §7.3 decomposes it, admission + latch + publish + D + present); or,
//                  whichever comes first, the kinematic criterion on the rendered pose: takeoff +0.3 m, goto velocity
//                  along the target direction >= 0.5 m/s, land -0.3 m, hover |v| <= 0.3 m/s
//   focusJumpM     at focus-set entry or exit: |p_render - (p_prev + v_prev dt)|
//   holdFrames     frames with a visible vehicle in HOLD; extrapFrames: focus or selected vehicle extrapolating
// Zero allocation on the per-frame path (pending commands live in a fixed pool of 8).
import { FlightState, Owner } from '@awr/contracts/enums'
import { pushRing, type AwrPerf } from './probe'
import { hypot3 } from '../hypot'

export type CmdKind = 'takeoff' | 'goto' | 'land' | 'hover'
interface Pending { active: boolean; agentNo: number; kind: CmdKind; t0: number; z0: number; tx: number; ty: number; tz: number; state0: number; ctrl0: number; effectS: number }
/** flight state (low 5 bits of the state byte) that shows takeoff and land; hover and goto show as a state-byte (sub-state) or owner switch */
export const CMD_STATE: Readonly<Record<CmdKind, number>> = { takeoff: FlightState.TAKING_OFF, goto: -1, land: FlightState.LANDING, hover: -1 }

/** whether (state, ctrl) shows the effect of kind relative to (state0, ctrl0) shown at mark time */
export function cmdEffectShown(kind: CmdKind, state: number, ctrl: number, state0: number, ctrl0: number): boolean {
  if (state < 0 || state0 < 0) return false
  const fs = state & 31
  const fs0 = state0 & 31
  const want = CMD_STATE[kind]
  if (want >= 0) return fs === want && fs0 !== want
  // hover and goto switch the FLYING sub-state (HOVER, GOTO: bits 5-7 of the state byte) or the control owner
  const op = ctrl >= 0 && (ctrl & 7) === Owner.OPERATOR && ctrl0 >= 0 && (ctrl0 & 7) !== Owner.OPERATOR
  return op || state !== state0
}

export const LATENCY = { climbM: 0.3, speedMps: 0.5, hoverMps: 0.3, timeoutMs: 30000 } as const

export class LatencyMeter {
  private lastPoseSimS = Number.NaN
  private lastAgent = -1
  private readonly pend: Pending[] = Array.from({ length: 8 }, () => ({ active: false, agentNo: -1, kind: 'goto' as CmdKind, t0: 0, z0: 0, tx: 0, ty: 0, tz: 0, state0: -1, ctrl0: -1, effectS: Number.NaN }))
  /** pending commands per agent (the drones phase checks these vehicles besides the focus or primary one) */
  private readonly pendN = new Uint8Array(65536)

  /** true while a command for agentNo awaits its visible effect */
  isPending(agentNo: number): boolean {
    return this.pendN[agentNo & 0xffff] > 0
  }
  constructor(private readonly p: AwrPerf) {}

  /** telemetry phase: simNow (s) and rate of this rAF */
  frameStart(simNowS: number, rate: number, advancing: boolean): void {
    if (!advancing || Number.isNaN(this.lastPoseSimS) || this.lastAgent < 0) return
    const ms = ((simNowS - this.lastPoseSimS) / Math.max(rate, 1e-3)) * 1000
    if (ms >= 0 && ms < 10000) pushRing(this.p.latency.tSimToPixelMs, ms)
  }

  /** drones phase: the pose of agent presented this frame was sampled at tPoseS (tRender or tFocus); -1 = none */
  presented(agentNo: number, tPoseS: number, hold: boolean, extrapolating: boolean): void {
    this.lastAgent = agentNo
    this.lastPoseSimS = agentNo >= 0 ? tPoseS : Number.NaN
    if (agentNo >= 0 && !hold && extrapolating) this.p.latency.extrapFrames++
  }

  holdFrame(): void {
    this.p.latency.holdFrames++
  }

  focusJump(m: number): void {
    if (Number.isFinite(m)) pushRing(this.p.latency.focusJumpM, m)
  }

  /** mark('cmd.sent', {agent, kind}) with the rendered position at mark time and the goto target (ENU) */
  markCmd(agentNo: number, kind: CmdKind, nowMs: number, z0: number, target?: ArrayLike<number>): void {
    // a newer command supersedes an older one of the same vehicle that has shown no effect yet: drop it unrecorded, so
    // the newer command's effect is not attributed to it
    for (const x of this.pend) if (x.active && x.agentNo === agentNo) this.close(x)
    let slot = this.pend.find((x) => !x.active)
    if (!slot) slot = this.pend.reduce((a, b) => (a.t0 <= b.t0 ? a : b))
    if (slot.active) this.close(slot)
    slot.active = true
    this.pendN[agentNo & 0xffff]++
    slot.agentNo = agentNo
    slot.kind = kind
    slot.t0 = nowMs
    slot.z0 = z0
    slot.tx = target ? target[0] : 0
    slot.ty = target ? target[1] : 0
    slot.tz = target ? target[2] : 0
    slot.state0 = -1
    slot.ctrl0 = -1
    slot.effectS = Number.NaN
    this.p.mark('cmd.sent')
  }

  /**
   * drones phase: rendered pose of agentNo (pos ENU, vel ENU), its newest flight state byte and control byte, the
   * simulation time (s) of the sample that switched to them and the rendered time tPoseS (s); closes pending commands
   * whose criterion holds
   */
  check(agentNo: number, nowMs: number, px: number, py: number, pz: number, vx: number, vy: number, vz: number,
    state = -1, stateSinceS = Number.NaN, tPoseS = Number.NaN, ctrl = -1): void {
    for (const s of this.pend) {
      if (!s.active || s.agentNo !== agentNo) continue
      if (nowMs - s.t0 > LATENCY.timeoutMs) {
        this.close(s)
        continue
      }
      // the state shown when the command left (first check after the mark)
      if (s.state0 < 0) {
        s.state0 = state
        s.ctrl0 = ctrl
      }
      // the sample time of the effect is kept once seen: a switch that the vehicle reverts before the rendered time reaches
      // it (an operator hover cancelled by the scenario) still shows when tPose passes that sample
      if (Number.isNaN(s.effectS) && cmdEffectShown(s.kind, state, ctrl, s.state0, s.ctrl0) && Number.isFinite(stateSinceS)) s.effectS = stateSinceS
      let ok = Number.isFinite(s.effectS) && Number.isFinite(tPoseS) && tPoseS >= s.effectS
      if (s.kind === 'takeoff') ok ||= pz - s.z0 >= LATENCY.climbM
      else if (s.kind === 'land') ok ||= s.z0 - pz >= LATENCY.climbM
      else if (s.kind === 'hover') ok ||= hypot3(vx, vy, vz) <= LATENCY.hoverMps
      else {
        const dx = s.tx - px
        const dy = s.ty - py
        const dz = s.tz - pz
        const l = hypot3(dx, dy, dz)
        ok ||= l > 1e-3 && (vx * dx + vy * dy + vz * dz) / l >= LATENCY.speedMps
      }
      if (ok) {
        this.close(s)
        pushRing(this.p.latency.cmdToVisibleMs, nowMs - s.t0)
      }
    }
  }

  private close(s: Pending): void {
    s.active = false
    const a = s.agentNo & 0xffff
    if (this.pendN[a] > 0) this.pendN[a]--
  }
}
