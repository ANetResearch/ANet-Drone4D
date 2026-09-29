// M06-AC-054 metric definitions (M06 §6.16; ADR-046; AWR-18 §7.3): tSimToPixel = (simNow of this rAF - sim time of the
// pose presented in the previous frame) / rate; cmdToVisible criteria (takeoff +0.3 m, land -0.3 m, goto velocity along
// the target direction >= 0.5 m/s, hover |v| <= 0.3 m/s); extrapolation and HOLD frame counters; focus jump samples.
import { describe, expect, it } from 'vitest'
import { lastOf, LatencyMeter } from '@/engine'
import { makeProbe, type AwrPerf } from '@/engine/perf/probe'

describe('latency metrics (M06-AC-054)', () => {
  it('tSimToPixel in wall ms at rate 1 and 10', () => {
    const p = makeProbe(false) as AwrPerf
    const m = new LatencyMeter(p)
    m.presented(3, 99.9, false, false) // pose of t = 99.9 s shown in the previous frame
    m.frameStart(100.0, 1, true)
    expect(lastOf(p.latency.tSimToPixelMs)).toBeCloseTo(100, 6)
    m.presented(3, 99.0, false, true)
    m.frameStart(100.0, 10, true)
    expect(lastOf(p.latency.tSimToPixelMs)).toBeCloseTo(100, 6)
    expect(p.latency.extrapFrames).toBe(1)
    m.presented(-1, 0, false, false)
    const n = p.latency.tSimToPixelMs.n
    m.frameStart(101, 1, true)
    expect(p.latency.tSimToPixelMs.n).toBe(n)
  })

  it('cmdToVisible criteria', () => {
    const p = makeProbe(false) as AwrPerf
    const m = new LatencyMeter(p)
    m.markCmd(1, 'takeoff', 1000, 0.3)
    m.check(1, 1100, 0, 0, 0.5, 0, 0, 0.5)
    expect(p.latency.cmdToVisibleMs.n).toBe(0)
    m.check(1, 1200, 0, 0, 0.61, 0, 0, 0.5)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(200)
    m.markCmd(1, 'goto', 2000, 10, [100, 0, 10])
    m.check(1, 2100, 0, 0, 10, 0, 0.4, 0)
    m.check(1, 2150, 0, 0, 10, 0.6, 0, 0)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(150)
    m.markCmd(1, 'land', 3000, 10)
    m.check(1, 3300, 0, 0, 9.6, 0, 0, -1)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(300)
    m.markCmd(1, 'hover', 4000, 10)
    m.check(1, 4100, 0, 0, 10, 0.2, 0.1, 0)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(100)
    expect(p.marks['cmd.sent']).toBeGreaterThan(0)
    m.focusJump(0.2)
    m.holdFrame()
    expect(lastOf(p.latency.focusJumpM)).toBe(0.2)
    expect(p.latency.holdFrames).toBe(1)
  })
})
