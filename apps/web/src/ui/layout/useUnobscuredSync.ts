// Commit points for the unobscured rect (M15-FR-015, §6.4.2): open, close, drag end, window resize and breakpoint
// changes call viewport.setUnobscuredRect with the panel durations (open 400 ms, close 350 ms, drag end and resize
// 250 ms, reduced 0) and write --uo-x/y/w/h on the overlay root so HUD, toolbars and readouts move by transform only.
// Nothing is committed while a separator is being dragged.
import { viewport } from '@/viewport/facade'
import { MOTION } from '@/lib/tokens/motion.gen'
import { getMotionTier } from '@/ui/motion/tier'
import { UX } from '@/ui/testing/uxProbe'
import { computeUnobscured, rectEq, type LayoutState, type Rect } from './unobscured'

export type CommitReason = 'open' | 'close' | 'dragEnd' | 'resize' | 'breakpoint'
const scratch: Rect = { x: 0, y: 0, w: 0, h: 0 }
const last: Rect = { x: -1, y: -1, w: -1, h: -1 }

export function commitUnobscured(state: LayoutState, reason: CommitReason, root: HTMLElement | null = typeof document !== 'undefined' ? document.documentElement : null): boolean {
  const W = globalThis.innerWidth || 0
  const H = globalThis.innerHeight || 0
  const rect = computeUnobscured(W, H, state, scratch)
  if (rectEq(rect, last)) return false
  const tier = getMotionTier()
  const durationMs = tier === 'reduced' || tier === 'off' ? 0
    : reason === 'open' ? MOTION.panelOpenMs : reason === 'close' ? MOTION.panelCloseMs : MOTION.durationFastMs
  viewport.setUnobscuredRect(rect, { durationMs, ease: 'smooth-out' })
  if (root) {
    root.style.setProperty('--uo-x', `${rect.x}px`)
    root.style.setProperty('--uo-y', `${rect.y}px`)
    root.style.setProperty('--uo-w', `${rect.w}px`)
    root.style.setProperty('--uo-h', `${rect.h}px`)
  }
  last.x = rect.x
  last.y = rect.y
  last.w = rect.w
  last.h = rect.h
  UX.layout.unobscured.x = rect.x
  UX.layout.unobscured.y = rect.y
  UX.layout.unobscured.w = rect.w
  UX.layout.unobscured.h = rect.h
  UX.layout.breakpoint = state.breakpoint
  UX.layout.unobscuredCommits++
  return true
}
