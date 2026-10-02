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

  it('cmdToVisible by state: the command effect shown once the rendered time reaches the switching sample (FX2-R3)', () => {
    const p = makeProbe(false) as AwrPerf
    const m = new LatencyMeter(p)
    const FLYING = 5
    const LANDING = 9
    const MISSION_CTRL = 2 | (2 << 4) // owner MISSION, native COMMAND
    const OPERATOR_CTRL = 1 | (2 << 4)
    // hover at 6 m/s: the kinematic criterion would wait for the deceleration; the OPERATOR owner is the effect
    m.markCmd(2, 'hover', 1000, 50)
    m.check(2, 1016, 0, 0, 50, 6, 0, 0, FLYING, 99.0, 99.8, MISSION_CTRL) // shown at mark time
    m.check(2, 1100, 0, 0, 50, 6, 0, 0, FLYING, 100.05, 99.9, OPERATOR_CTRL) // switch arrived (100.05 s), not yet rendered
    expect(p.latency.cmdToVisibleMs.n).toBe(0)
    m.check(2, 1250, 0, 0, 50, 5.5, 0, 0, FLYING, 100.05, 100.06, OPERATOR_CTRL)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(250)
    // a second hover on an operator-owned vehicle with an unchanged state byte: the kinematic criterion closes it
    m.markCmd(2, 'hover', 2000, 50)
    m.check(2, 2050, 0, 0, 50, 2, 0, 0, FLYING, 100.05, 101, OPERATOR_CTRL)
    expect(p.latency.cmdToVisibleMs.n).toBe(1)
    m.check(2, 2900, 0, 0, 50, 0.1, 0, 0, FLYING, 100.05, 102, OPERATOR_CTRL)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(900)
    // land: LANDING (state byte with sub bits) shown at the rendered time
    m.markCmd(2, 'land', 3000, 50)
    m.check(2, 3010, 0, 0, 50, 0, 0, 0, FLYING, 100.05, 103, OPERATOR_CTRL)
    m.check(2, 3200, 0, 0, 50, 0, 0, 0, LANDING | (1 << 5), 103.1, 103.2, OPERATOR_CTRL)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(200)
    // a switch reverted before the rendered time reaches it still closes the command once tPose passes its sample
    m.markCmd(3, 'hover', 4000, 50)
    m.check(3, 4010, 0, 0, 50, 6, 0, 0, FLYING, 99, 103.9, MISSION_CTRL)
    m.check(3, 4100, 0, 0, 50, 6, 0, 0, FLYING, 104.0, 103.95, OPERATOR_CTRL) // owner OPERATOR from 104.0 s
    m.check(3, 4150, 0, 0, 50, 6, 0, 0, FLYING, 104.1, 103.98, MISSION_CTRL) // the scenario took it back at 104.1 s
    expect(p.latency.cmdToVisibleMs.n).toBe(3)
    m.check(3, 4250, 0, 0, 50, 6, 0, 0, FLYING, 104.1, 104.02, MISSION_CTRL)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(250)
    // hover as a FLYING sub-state switch (sub HOVER in bits 5-7) with the owner unchanged (lock-exempt safety op)
    m.markCmd(4, 'hover', 5000, 50)
    m.check(4, 5010, 0, 0, 50, 6, 0, 0, FLYING | (1 << 5), 99, 104.9, MISSION_CTRL)
    m.check(4, 5100, 0, 0, 50, 6, 0, 0, FLYING | (3 << 5), 105.0, 104.95, MISSION_CTRL)
    m.check(4, 5300, 0, 0, 50, 5, 0, 0, FLYING | (3 << 5), 105.0, 105.01, MISSION_CTRL)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(300)
    // a command without effect is superseded by the next one of the same vehicle (not closed by the newer effect)
    const n0 = p.latency.cmdToVisibleMs.n
    m.markCmd(5, 'hover', 6000, 50)
    m.check(5, 6010, 0, 0, 50, 6, 0, 0, FLYING | (3 << 5), 99, 105.9, MISSION_CTRL)
    m.markCmd(5, 'hover', 9000, 50)
    m.check(5, 9010, 0, 0, 50, 6, 0, 0, FLYING | (3 << 5), 99, 108.9, MISSION_CTRL)
    m.check(5, 9100, 0, 0, 50, 6, 0, 0, FLYING | (1 << 5), 109.0, 109.05, MISSION_CTRL)
    expect(p.latency.cmdToVisibleMs.n).toBe(n0 + 1)
    expect(lastOf(p.latency.cmdToVisibleMs)).toBe(100)
  })
})
