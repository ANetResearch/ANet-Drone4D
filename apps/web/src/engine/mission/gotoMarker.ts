// GoTo target marker (M06-FR-045, AC-034; AWR-14 §6.7). Owner: M06.
// Drawn through the shared batches (no program of its own): the GlyphLayer symbol and a plumb line from the picked
// surface point to the target in the overlay thin-line batch.
//   preview    cmd.goto symbol (ring + centre dot) + plumb line, foreground g50
//   accepted   outline ring (g50)
//   running    solid disc in the foreground colour + ring
//   succeeded  as running, held input.resultHoldMs (1500 ms), then faded over --duration-quick
//   failed     r500 outline + triangle alert, kept until the next operation; the red obeys the viewport RedArbiter
//              (g50 when another entity owns the red)
// The "E . N . height" readout is exposed for the LabelLayer (GoTo readout priority). Layer frame ENU.
import { INPUT } from '@/lib/tokens/input.gen'
import { MOTION } from '@/lib/tokens/motion.gen'
import { GlyphClass, Palette, Shape, type GlyphLayer } from '../drones/glyph/GlyphLayer'
import type { ThinLineBatch } from './lineBatch'

export type GotoState = 'preview' | 'accepted' | 'running' | 'succeeded' | 'failed'

export const GOTO_MARKER = { ringCssPx: 18, discCssPx: 9, alertCssPx: 12 } as const

export class GotoMarker {
  state: GotoState | null = null
  private since = 0
  readonly target = new Float64Array(3)
  readonly surface = new Float64Array(3)
  /** alpha of the last push (fade after succeeded); 0 when not drawn */
  alpha = 0

  /** surface hit and target point (ENU m) */
  set(surface: ArrayLike<number>, target: ArrayLike<number>, state: GotoState, nowMs: number): void {
    for (let i = 0; i < 3; i++) {
      this.surface[i] = surface[i]
      this.target[i] = target[i]
    }
    this.setState(state, nowMs)
  }

  setState(state: GotoState, nowMs: number): void {
    this.state = state
    this.since = nowMs
  }

  clear(): void {
    this.state = null
    this.alpha = 0
  }

  get visible(): boolean {
    return this.state !== null
  }

  /** world phase: stage the symbol and the plumb line; redAllowed = no other red owner (RedArbiter) */
  push(g: GlyphLayer, lines: ThinLineBatch, nowMs: number, redAllowed: boolean): void {
    const s = this.state
    if (s === null) {
      this.alpha = 0
      return
    }
    let a = 1
    if (s === 'succeeded') {
      const t = nowMs - this.since - INPUT.resultHoldMs
      if (t > 0) a = Math.max(0, 1 - t / MOTION.durationQuickMs)
      if (a <= 0) {
        this.clear()
        return
      }
    }
    this.alpha = a
    const x = this.target[0]
    const y = this.target[1]
    const z = this.target[2]
    const R = GOTO_MARKER.ringCssPx
    const failed = s === 'failed'
    const col = failed && redAllowed ? Palette.R500 : Palette.G50
    lines.seg(this.surface[0], this.surface[1], this.surface[2], x, y, z, col, false, 0.9 * a)
    if (s === 'preview') g.push(GlyphClass.Mission, x, y, z, R, Shape.Goto, 1.5, Palette.G50, a)
    else if (s === 'accepted') g.push(GlyphClass.Mission, x, y, z, R, Shape.Ring, 1.5, Palette.G50, a)
    else if (s === 'running' || s === 'succeeded') {
      g.push(GlyphClass.Mission, x, y, z, R, Shape.Ring, 1.5, Palette.G50, a)
      g.push(GlyphClass.Mission, x, y, z, GOTO_MARKER.discCssPx, Shape.Disc, 1, Palette.G50, a)
    } else {
      g.push(failed && redAllowed ? GlyphClass.Red : GlyphClass.Mission, x, y, z, R, Shape.Ring, 1.5, col, a)
      g.push(failed && redAllowed ? GlyphClass.Red : GlyphClass.Mission, x, y, z, GOTO_MARKER.alertCssPx, Shape.Triangle, 1.5, col, a)
    }
  }
}
