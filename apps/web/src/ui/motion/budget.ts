// DOM motion budget executor (M15-FR-057; ADR-029; d02 §4.7): decided before an animation starts; over budget means a
// degraded animation, never a queue. Blur animations <= 12 at once (others play without blur); number pop-in <= 24 per
// second site wide (token bucket; others assign directly); resident loops (shimmer, skeleton pulse, spinner, matrix)
// <= 2 on screen, by priority, off-screen loops pause.
import { MOTION_BUDGET } from '@/lib/tokens/input.gen'
import { UX } from '@/ui/testing/uxProbe'

export interface LoopHandle {
  priority: number
  onScreen: boolean
  pause(): void
  resume(): void
}

let blurActive = 0
let popTokens: number = MOTION_BUDGET.popPerS
let lastRefill = 0
let popWindowStart = 0
let popInWindow = 0
const loops = new Set<LoopHandle>()
const waiting = new Set<LoopHandle>()

export const motionBudget = {
  get blurActive() {
    return blurActive
  },
  get loopsActive() {
    return loops.size
  },
  /** reserve a blur animation slot for `ms`; false means play without blur */
  acquireBlur(ms: number): boolean {
    if (blurActive >= MOTION_BUDGET.blurMax) return false
    blurActive++
    if (blurActive > UX.budget.blurMax) UX.budget.blurMax = blurActive
    setTimeout(() => {
      blurActive--
    }, ms)
    return true
  },
  /** token bucket, 24 per second, capacity 24; false means assign the value without the pop-in */
  acquirePop(nowMs: number): boolean {
    popTokens = Math.min(MOTION_BUDGET.popPerS, popTokens + ((nowMs - lastRefill) * MOTION_BUDGET.popPerS) / 1000)
    lastRefill = nowMs
    if (nowMs - popWindowStart >= 1000) {
      popWindowStart = nowMs
      popInWindow = 0
    }
    if (popTokens < 1) return false
    popTokens -= 1
    popInWindow++
    if (popInWindow > UX.budget.popPerSecMax) UX.budget.popPerSecMax = popInWindow
    return true
  },
  /** ask for a resident loop slot; the weakest running loop yields to a higher priority one */
  requestLoop(h: LoopHandle): boolean {
    if (!h.onScreen) {
      waiting.add(h)
      return false
    }
    if (loops.has(h)) return true
    if (loops.size < MOTION_BUDGET.loopMax) {
      loops.add(h)
      waiting.delete(h)
      if (loops.size > UX.budget.loopsMax) UX.budget.loopsMax = loops.size
      return true
    }
    let weakest: LoopHandle | null = null
    for (const l of loops) if (!weakest || l.priority < weakest.priority) weakest = l
    if (weakest && weakest.priority < h.priority) {
      weakest.pause()
      loops.delete(weakest)
      waiting.add(weakest)
      loops.add(h)
      waiting.delete(h)
      return true
    }
    waiting.add(h)
    return false
  },
  releaseLoop(h: LoopHandle): void {
    loops.delete(h)
    waiting.delete(h)
    let best: LoopHandle | null = null
    for (const w of waiting) if (w.onScreen && (!best || w.priority > best.priority)) best = w
    if (best && loops.size < MOTION_BUDGET.loopMax) {
      loops.add(best)
      waiting.delete(best)
      best.resume()
    }
  },
  /** tests */
  reset(): void {
    blurActive = 0
    popTokens = MOTION_BUDGET.popPerS
    lastRefill = 0
    loops.clear()
    waiting.clear()
  },
}
