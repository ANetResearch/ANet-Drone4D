// Precipitation anchor octaves (M07-FR-041; M07 §6.8.2 item 6; r16 §3.2.6). Owner: M07.
// The box of stateless drops sits between the camera and the orbit focus: k = smoothstep(80, 400, camera AGL), anchor =
// lerp(camera, focus, k), anchor height = lerp(camera z, ground(focus) + 0.35 H_level, k). The octave (R/H = 20/20,
// 60/48, 180/120, 540/320 m) follows s = max(camera AGL, 0.35 |camera - focus|) with hysteresis 0.8 / 1.25; on a switch
// the old and new octave are drawn together and cross-faded over --duration-very-slow with the drop count split 60/40
// (total unchanged). Follow and FPV force octave 0. Zero allocation.
import { MOTION } from '@/lib/tokens/motion.gen'
import { smoothstep } from '../state/derive'
import { ENV_TIERS } from '../quality/envTiers'

export class PrecipAnchor {
  level = 0
  /** previous level while cross-fading, -1 otherwise */
  prevLevel = -1
  private t0 = Number.NaN
  /** fade of the current level 0..1 (the previous level gets 1 - fade) */
  fade = 1
  readonly anchor = new Float64Array(3)
  readonly prevAnchor = new Float64Array(3)
  switches = 0
  reduced = false

  /** the octave for a scale s with hysteresis around the current octave */
  static octaveFor(s: number, cur: number): number {
    const R = ENV_TIERS.precipR
    let lvl = cur
    while (lvl < R.length - 1 && s > R[lvl + 1] * ENV_TIERS.hystUp * 0.5) lvl++
    while (lvl > 0 && s < R[lvl] * ENV_TIERS.hystDown * 0.5) lvl--
    return lvl
  }

  update(cam: ArrayLike<number>, focus: ArrayLike<number>, camAgl: number, groundFocus: number, forceNear: boolean, nowMs: number): void {
    const dist = Math.hypot(cam[0] - focus[0], cam[1] - focus[1], cam[2] - focus[2])
    const s = Math.max(camAgl, ENV_TIERS.anchorFocusFrac * dist)
    const want = forceNear ? 0 : PrecipAnchor.octaveFor(s, this.level)
    if (want !== this.level) {
      this.prevLevel = this.level
      this.prevAnchor.set(this.anchor)
      this.level = want
      this.t0 = nowMs
      this.switches++
    }
    if (this.prevLevel >= 0) {
      const x = this.reduced ? 1 : (nowMs - this.t0) / MOTION.durationVerySlowMs
      this.fade = Math.min(Math.max(x, 0), 1)
      if (this.fade >= 1) this.prevLevel = -1
    } else this.fade = 1
    const k = forceNear ? 0 : smoothstep(ENV_TIERS.anchorAglLo, ENV_TIERS.anchorAglHi, camAgl)
    const H = ENV_TIERS.precipH[this.level]
    this.anchor[0] = cam[0] + (focus[0] - cam[0]) * k
    this.anchor[1] = cam[1] + (focus[1] - cam[1]) * k
    this.anchor[2] = cam[2] + (groundFocus + ENV_TIERS.anchorFocusFrac * H - cam[2]) * k
  }

  get R(): number {
    return ENV_TIERS.precipR[this.level]
  }
  get H(): number {
    return ENV_TIERS.precipH[this.level]
  }
}
