// Camera flight (M06-FR-050, AC-038; ADR-029; AWR-14 §6.5). Owner: M06.
// T = clamp(0.4 + 0.15 ln(1 + d / 20), T_MIN, T_MAX) s with d the larger of the eye and target displacements (m) and
// T_MIN / T_MAX the --duration-camera-min/max tokens (400 / 1200 ms); the curve is --ease-smooth-out evaluated by
// engine/anim/bezier. Each camera-phase call interpolates eye and target (ENU) between A and B (the destination may be
// a moving target, re-read every frame for Third and FPV entries). Any user input cancels; reduced and off motion
// tiers cut directly to B (the caller checks the tier).
import { EASE, MOTION } from '@/lib/tokens/motion.gen'
import { bezierAt } from '../anim/bezier'
import { hypot3 } from '../hypot'

export type FlightReason = 'mode' | 'focus' | 'viewcube' | 'home' | 'dblclick' | 'north' | 'pose'

/** flight duration in seconds for a displacement of d metres */
export function flightDurationS(d: number): number {
  const t = 0.4 + 0.15 * Math.log(1 + Math.max(0, d) / 20)
  return Math.min(MOTION.cameraMaxMs / 1000, Math.max(MOTION.cameraMinMs / 1000, t))
}

export class CameraFlight {
  readonly a = new Float64Array(6) // eye xyz, target xyz (ENU)
  readonly b = new Float64Array(6)
  readonly out = new Float64Array(6)
  active = false
  t0 = 0
  durMs = 0
  reason: FlightReason = 'mode'
  /** optional moving destination (Third/FPV entry): refreshes b every frame */
  dest: ((b: Float64Array) => boolean) | null = null

  start(from: ArrayLike<number>, to: ArrayLike<number>, nowMs: number, reason: FlightReason, dest: ((b: Float64Array) => boolean) | null = null): number {
    for (let i = 0; i < 6; i++) {
      this.a[i] = from[i]
      this.b[i] = to[i]
    }
    const d = Math.max(hypot3(to[0] - from[0], to[1] - from[1], to[2] - from[2]), hypot3(to[3] - from[3], to[4] - from[4], to[5] - from[5]))
    this.durMs = flightDurationS(d) * 1000
    this.t0 = nowMs
    this.active = true
    this.reason = reason
    this.dest = dest
    return this.durMs
  }

  /** eased pose at nowMs into out: 0 idle (out untouched), 1 flying, 2 finished in this call (out = b) */
  step(nowMs: number): 0 | 1 | 2 {
    if (!this.active) return 0
    if (this.dest) this.dest(this.b)
    const u = this.durMs <= 0 ? 1 : Math.min(1, (nowMs - this.t0) / this.durMs)
    const e = bezierAt(EASE.smoothOut, u)
    for (let i = 0; i < 6; i++) this.out[i] = this.a[i] + (this.b[i] - this.a[i]) * e
    if (u >= 1) {
      this.active = false
      this.dest = null
      return 2
    }
    return 1
  }

  cancel(): void {
    this.active = false
    this.dest = null
  }
}
