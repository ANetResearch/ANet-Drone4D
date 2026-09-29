// Client short damping and anchor smoothing (M07-FR-031; M07 §6.3.15). Owner: M07.
// Only visual uniforms of a step change are cross-faded (--duration-slow with --ease-smooth-out, from motion tokens);
// transitions of smooth/exp frames are already continuous by construction, epoch changes snap. Anchor corrections below
// 1 m (wetness 0.02) are spread linearly over --duration-very-slow, larger ones snap. The reduced motion tier switches
// directly. HUD numbers, anchors used for physics-like readings and the keyframe itself are never damped.
import { EASE, MOTION } from '@/lib/tokens/motion.gen'
import { bezierAt } from '../../anim/bezier'
import { mod, shortestArc } from './conventions'
import { NF, type PresetsModel } from './presets'

export const DAMP_MS = MOTION.durationSlowMs
export const SMOOTH_MS = MOTION.durationVerySlowMs

export class VisualDamper {
  /** displayed visual scalars */
  readonly vis = new Float64Array(NF)
  private readonly from = new Float64Array(NF)
  private t0 = Number.NaN
  reduced = false
  active = false

  /** a step change became current at nowMs: fade from the currently displayed values */
  start(nowMs: number): void {
    if (this.reduced) return
    this.from.set(this.vis)
    this.t0 = nowMs
    this.active = true
  }

  snap(target: Float64Array): void {
    this.vis.set(target)
    this.active = false
  }

  update(target: Float64Array, nowMs: number, P: PresetsModel): void {
    if (!this.active) {
      this.vis.set(target)
      return
    }
    const x = (nowMs - this.t0) / DAMP_MS
    if (x >= 1 || !(x >= 0)) {
      this.active = false
      this.vis.set(target)
      return
    }
    const k = bezierAt(EASE.smoothOut, x)
    for (let i = 0; i < NF; i++) {
      const a = this.from[i]
      const b = target[i]
      const sp = P.space[i]
      this.vis[i] = sp === 1 ? Math.exp(Math.log(a) + (Math.log(b) - Math.log(a)) * k) : sp === 2 ? mod(a + shortestArc(a, b) * k, 360) : a + (b - a) * k
    }
  }
}

/** additive correction that decays linearly to zero over SMOOTH_MS (visual anchors only) */
export class AnchorSmoother {
  /** [s, dx, dy, dz, fallRain, fallSnow, wetness, puddle] */
  readonly off = new Float64Array(8)
  private readonly start = new Float64Array(8)
  private t0 = Number.NaN

  begin(err: Float64Array, nowMs: number): void {
    this.start.set(err)
    this.t0 = nowMs
  }

  clear(): void {
    this.off.fill(0)
    this.start.fill(0)
    this.t0 = Number.NaN
  }

  update(nowMs: number): Float64Array {
    if (Number.isNaN(this.t0)) return this.off
    const k = 1 - (nowMs - this.t0) / SMOOTH_MS
    if (k <= 0) {
      this.clear()
      return this.off
    }
    for (let i = 0; i < 8; i++) this.off[i] = this.start[i] * k
    return this.off
  }
}
