// M15-FR-052..057 (ADR-029; d02 §4.7): motion tier resolution, the digit diff of MotionNumber and the DOM motion budget.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { resolveTier, userMotionToTier } from '@/ui/motion/tier'
import { motionBudget, type LoopHandle } from '@/ui/motion/budget'
import { changedDigits } from '@/ui/motion/MotionNumber'
import { morphBudget } from '@/ui/icons/morphBudget'
import { MOTION_BUDGET } from '@/lib/tokens/input.gen'
import { PERF_UI } from '@/ui/shell/perfUi'

describe('resolveTier', () => {
  it('takes the most restrictive of os, user and governor', () => {
    expect(resolveTier({ os: 'full', user: 'full', gov: 'full', test: null })).toEqual({ tier: 'full', source: 'os' })
    expect(resolveTier({ os: 'full', user: 'lite', gov: 'full', test: null })).toEqual({ tier: 'lite', source: 'user' })
    expect(resolveTier({ os: 'reduced', user: 'lite', gov: 'lite', test: null })).toEqual({ tier: 'reduced', source: 'os' })
    expect(resolveTier({ os: 'full', user: 'full', gov: 'lite', test: null })).toEqual({ tier: 'lite', source: 'governor' })
  })

  it('lets the test switch override everything', () => {
    expect(resolveTier({ os: 'full', user: 'full', gov: 'full', test: 'off' })).toEqual({ tier: 'off', source: 'test' })
  })

  it('maps the user setting "system" to no restriction', () => {
    expect(userMotionToTier('system')).toBe('full')
    expect(userMotionToTier('reduced')).toBe('reduced')
  })
})

describe('changedDigits', () => {
  it('marks only the digits that changed, aligned from the right', () => {
    expect(changedDigits('128', '129')).toEqual([false, false, true])
    expect(changedDigits('99', '100')).toEqual([true, true, true])
    expect(changedDigits('4.82M', '4.83M')).toEqual([false, false, false, true, false])
  })

  it('compares separators and units by position too (only digits animate)', () => {
    expect(changedDigits('1.0 ms', '2.0 ms')).toEqual([true, false, false, false, false, false])
  })
})

describe('motionBudget', () => {
  afterEach(() => {
    vi.useRealTimers()
    motionBudget.reset()
  })

  it('caps concurrent blur animations and frees slots after their duration', () => {
    vi.useFakeTimers()
    let granted = 0
    for (let i = 0; i < MOTION_BUDGET.blurMax + 5; i++) if (motionBudget.acquireBlur(100)) granted++
    expect(granted).toBe(MOTION_BUDGET.blurMax)
    vi.advanceTimersByTime(101)
    expect(motionBudget.blurActive).toBe(0)
    expect(motionBudget.acquireBlur(100)).toBe(true)
  })

  it('limits number pop-ins per second with a token bucket', () => {
    const t0 = 10_000
    let ok = 0
    for (let i = 0; i < 100; i++) if (motionBudget.acquirePop(t0)) ok++
    expect(ok).toBeLessThanOrEqual(MOTION_BUDGET.popPerS)
    expect(motionBudget.acquirePop(t0 + 1000)).toBe(true)
  })

  it('keeps at most two resident loops and lets a higher priority take over', () => {
    const mk = (priority: number): LoopHandle & { paused: number } => {
      const h = { priority, onScreen: true, paused: 0, pause() { h.paused++ }, resume() {} }
      return h
    }
    const a = mk(1)
    const b = mk(2)
    const c = mk(3)
    const d = mk(0)
    expect(motionBudget.requestLoop(a)).toBe(true)
    expect(motionBudget.requestLoop(b)).toBe(true)
    expect(motionBudget.loopsActive).toBe(MOTION_BUDGET.loopMax)
    expect(motionBudget.requestLoop(c)).toBe(true)
    expect(a.paused).toBe(1)
    expect(motionBudget.requestLoop(d)).toBe(false)
    expect(motionBudget.loopsActive).toBe(2)
    const off = mk(9)
    off.onScreen = false
    expect(motionBudget.requestLoop(off)).toBe(false)
  })
})

describe('morphBudget (K = 8)', () => {
  it('denies morphs beyond K and counts them in __perf.ui.icons', () => {
    vi.useFakeTimers()
    morphBudget.reset()
    const denied0 = PERF_UI.icons.denied
    let ok = 0
    for (let i = 0; i < 12; i++) if (morphBudget.acquire(450)) ok++
    expect(ok).toBe(MOTION_BUDGET.morphK)
    expect(MOTION_BUDGET.morphK).toBe(8)
    expect(PERF_UI.icons.denied - denied0).toBe(4)
    vi.advanceTimersByTime(451)
    expect(morphBudget.active).toBe(0)
    vi.useRealTimers()
  })
})
