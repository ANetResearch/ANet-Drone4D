// Shared UI refresh tick (ADR-066; D1-AC-23): every periodic UI publisher writes in the same frame, Tier S at 4 Hz and
// Tier B/A at 10 Hz (the --telemetry-text-interval token), quantised to frame boundaries like loop fps tasks; publishers
// in later phases of the same frame (governor) agree with the overlay phase.
import { beforeEach, describe, expect, it } from 'vitest'
import { ctx, register, runPhase, type FrameCtx } from '@/engine/loop'
import { resetUiTick, uiTick, uiTickDue, uiTickPeriodMs } from '@/stores/uiTick'

const frame = (frameNo: number, nowMs: number, tier: FrameCtx['tier'] = 'S'): FrameCtx => ({ ...ctx, frameNo, nowMs, tier })

function drive(fps: number, seconds: number, tier: FrameCtx['tier']): { overlay: number[]; governor: number[]; times: number[] } {
  const out = { overlay: [] as number[], governor: [] as number[], times: [] as number[] }
  const offA = register('overlay', 't-ui-pub-a', (c) => {
    if (uiTickDue(c)) {
      out.overlay.push(c.frameNo)
      out.times.push(c.nowMs)
    }
  }, { order: 5 })
  const offB = register('governor', 't-ui-pub-b', (c) => {
    if (uiTickDue(c)) out.governor.push(c.frameNo)
  })
  const n = Math.round(fps * seconds)
  for (let k = 0; k < n; k++) {
    const f = frame(k + 1, (k * 1000) / fps, tier)
    runPhase('overlay', f)
    runPhase('governor', f)
  }
  offA()
  offB()
  return out
}

describe('stores/uiTick (ADR-066)', () => {
  beforeEach(() => resetUiTick())

  it('Tier S: 4 Hz at 60 fps and at 15 fps, every publisher of a frame in the same tick', () => {
    expect(uiTickPeriodMs('S')).toBe(250)
    const a = drive(60, 4, 'S')
    expect(a.overlay).toEqual(a.governor)
    expect(a.overlay.length).toBe(16)
    resetUiTick()
    const b = drive(15, 4, 'S')
    expect(b.overlay).toEqual(b.governor)
    // 66.7 ms frames: a tick every 4th frame (266.7 ms)
    expect(b.overlay.length).toBe(15)
    for (let i = 1; i < b.times.length; i++) expect(b.times[i] - b.times[i - 1]).toBeGreaterThanOrEqual(249)
  })

  it('Tier B/A: 10 Hz', () => {
    expect(uiTickPeriodMs('B')).toBe(100)
    const a = drive(60, 2, 'B')
    expect(a.overlay).toEqual(a.governor)
    expect(a.overlay.length).toBe(20)
  })

  it('a clock that jumps back (new session) ticks at once', () => {
    expect(uiTickDue(frame(1, 5000))).toBe(true)
    expect(uiTickDue(frame(2, 5100))).toBe(false)
    expect(uiTickDue(frame(3, 10))).toBe(true)
    expect(uiTick.count).toBe(2)
  })
})
