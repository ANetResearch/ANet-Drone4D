// Scalar tweens for the camera phase (view-offset centre, fades; M06 §6.11 unobscured rect, §8 motion). Owner: M06.
// Durations and curves come from lib/tokens/motion.gen.ts (ADR-029); reduced and off motion tiers jump to the end.
// A Tween is a preallocated object; start() and value() never allocate.
import { bezierAt, type Bezier } from './bezier'

export class Tween {
  from = 0
  to = 0
  t0 = 0
  durMs = 0
  ease: Bezier = [0, 0, 1, 1]

  start(from: number, to: number, nowMs: number, durMs: number, ease: Bezier): void {
    this.from = from
    this.to = to
    this.t0 = nowMs
    this.durMs = Math.max(0, durMs)
    this.ease = ease
  }
  /** set without animation */
  jump(v: number): void {
    this.from = v
    this.to = v
    this.durMs = 0
  }
  value(nowMs: number): number {
    if (this.durMs <= 0) return this.to
    const u = (nowMs - this.t0) / this.durMs
    if (u >= 1) return this.to
    return this.from + (this.to - this.from) * bezierAt(this.ease, Math.max(0, u))
  }
  done(nowMs: number): boolean {
    return this.durMs <= 0 || nowMs - this.t0 >= this.durMs
  }
}
