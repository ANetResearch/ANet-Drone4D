// Single-step glide (M12 FR-013, §6.5 "单步过渡"; ADR-029). Owner: M12. After STEPPING -> PAUSED with a step of at
// most 1 s of simulation time, the seen time moves from the previous value to the new TIME.t_sim over --duration-fast
// with --ease-smooth-out, so the interpolation ring (which holds the samples before and after the step) draws the
// vehicles along their Hermite curves instead of jumping. Reduced motion jumps. Presentation timing comes from the
// motion tokens (MOT-01).
import { EASE, MOTION } from '@/lib/tokens/motion.gen'
import { bezierAt } from '../anim/bezier'

export class StepGlide {
  active = false
  fromMs = 0
  toMs = 0
  private atMs = Number.NaN
  private pending = false

  /** request a glide; it starts on the next clock tick (frame time base) */
  request(fromMs: number, toMs: number, reduced: boolean): void {
    if (reduced || !(toMs > fromMs)) {
      this.cancel()
      return
    }
    this.fromMs = fromMs
    this.toMs = toMs
    this.pending = true
    this.active = true
  }

  cancel(): void {
    this.active = false
    this.pending = false
    this.atMs = Number.NaN
  }

  /** the glided simulation time (ms) at nowMs; ends exactly at toMs after --duration-fast */
  value(nowMs: number): number {
    if (!this.active) return this.toMs
    if (this.pending) {
      this.pending = false
      this.atMs = nowMs
    }
    const u = (nowMs - this.atMs) / MOTION.durationFastMs
    if (u >= 1) {
      this.active = false
      return this.toMs
    }
    return this.fromMs + (this.toMs - this.fromMs) * bezierAt(EASE.smoothOut, Math.max(0, u))
  }
}
