// Latency metrics (M06 §6.16, FR-078; ADR-046; AWR-18 §7.3, §9.3; D1-AC-26). Owner: M06.
//   tSimToPixelMs  focus vehicle (else the selected one): (simNow of this rAF - sim time of the pose presented in the
//                  previous frame) / rate, in wall ms; once per frame
//   cmdToVisibleMs mark('cmd.sent') to the first frame whose rendered pose satisfies the criterion: takeoff +0.3 m,
//                  goto velocity along the target direction >= 0.5 m/s, land -0.3 m, hover |v| <= 0.3 m/s
//   focusJumpM     at focus-set entry or exit: |p_render - (p_prev + v_prev dt)|
//   holdFrames     frames with a visible vehicle in HOLD; extrapFrames: focus or selected vehicle extrapolating
// Zero allocation on the per-frame path (pending commands live in a fixed pool of 8).
import { pushRing, type AwrPerf } from './probe'

export type CmdKind = 'takeoff' | 'goto' | 'land' | 'hover'
interface Pending { active: boolean; agentNo: number; kind: CmdKind; t0: number; z0: number; tx: number; ty: number; tz: number }

export const LATENCY = { climbM: 0.3, speedMps: 0.5, hoverMps: 0.3, timeoutMs: 30000 } as const

export class LatencyMeter {
  private lastPoseSimS = Number.NaN
  private lastAgent = -1
  private readonly pend: Pending[] = Array.from({ length: 8 }, () => ({ active: false, agentNo: -1, kind: 'goto' as CmdKind, t0: 0, z0: 0, tx: 0, ty: 0, tz: 0 }))
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
    let slot = this.pend.find((x) => !x.active)
    if (!slot) slot = this.pend.reduce((a, b) => (a.t0 <= b.t0 ? a : b))
    slot.active = true
    slot.agentNo = agentNo
    slot.kind = kind
    slot.t0 = nowMs
    slot.z0 = z0
    slot.tx = target ? target[0] : 0
    slot.ty = target ? target[1] : 0
    slot.tz = target ? target[2] : 0
    this.p.mark('cmd.sent')
  }

  /** drones phase: rendered pose of agentNo (pos ENU, vel ENU); closes pending commands whose criterion holds */
  check(agentNo: number, nowMs: number, px: number, py: number, pz: number, vx: number, vy: number, vz: number): void {
    for (const s of this.pend) {
      if (!s.active || s.agentNo !== agentNo) continue
      if (nowMs - s.t0 > LATENCY.timeoutMs) {
        s.active = false
        continue
      }
      let ok = false
      if (s.kind === 'takeoff') ok = pz - s.z0 >= LATENCY.climbM
      else if (s.kind === 'land') ok = s.z0 - pz >= LATENCY.climbM
      else if (s.kind === 'hover') ok = Math.hypot(vx, vy, vz) <= LATENCY.hoverMps
      else {
        const dx = s.tx - px
        const dy = s.ty - py
        const dz = s.tz - pz
        const l = Math.hypot(dx, dy, dz)
        ok = l > 1e-3 && (vx * dx + vy * dy + vz * dz) / l >= LATENCY.speedMps
      }
      if (ok) {
        s.active = false
        pushRing(this.p.latency.cmdToVisibleMs, nowMs - s.t0)
      }
    }
  }
}
