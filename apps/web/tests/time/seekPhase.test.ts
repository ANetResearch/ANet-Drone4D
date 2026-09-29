// M12-AC-048 (client part) / FR-048 / FR-011: after a replay seek the clock is frozen with D = 0, the seen time equals
// the requested t, and the vehicles rest on the backfill composite frame at t_b <= t (t - t_b < one 40 ms block). The
// new epoch while frozen raises the extrapolation limit to at least one block interval, so the marked vehicle's
// Full64 sample (velocity and body rate) extrapolates to t within 1 cm of the true trajectory (15 m/s turn, r = 30 m,
// the M12 §6.4 error table case "seek extrapolation <= 6 mm"). The environment phase part of AC-048 is M07's anchor
// advance (byte-exact EnvKeyframe backfill is tests/recorder/test_replay.py).
import { describe, expect, it } from 'vitest'
import { initTime, newDronePoseSoA, TIME_STATE as TS } from '@/engine/time/index'
import { makeFrame, stubRegister } from './helpers'

const R = 30
const V = 15
const W = V / R
const pos = (t: number): number[] => [R * Math.cos(W * t), R * Math.sin(W * t), 60]
const vel = (t: number): number[] => [-V * Math.sin(W * t), V * Math.cos(W * t), 0]
const quat = (t: number): number[] => {
  const yaw = W * t + Math.PI / 2
  return [0, 0, Math.sin(yaw / 2), Math.cos(yaw / 2)]
}

describe('seek landing (M12-AC-048 client part)', () => {
  it.each([5, 13, 27, 39])('t - t_b = %i ms: position within 1 cm, attitude within 0.1 deg, seen time = t', (dms) => {
    const reg = stubRegister()
    const rt = initTime({ register: reg.register, roster: () => null, reducedMotion: () => false })
    const f = makeFrame()
    // a paused first epoch
    f.time(TS.PAUSED | 0x80, 1, 1, 0, 0, 0)
    rt.ingest(f, 0)
    reg.run(16)
    rt.interp.eMaxMs = 10 // e.g. a fast live stream: the frozen epoch change must raise it to one block
    const tb = 420.0 // s, a 40 ms grid point
    const t = tb + dms / 1000
    f.clear()
    f.time(TS.PAUSED | 0x80, 2, 1, t * 1000, 0, 20)
    f.swarm1(tb * 1000, [{ a: 1, p: pos(tb), v: vel(tb), q: quat(tb) }])
    f.full1(1, 9, tb * 1000, pos(tb), vel(tb), quat(tb), [0, 0, W])
    rt.ingest(f, 20)
    const ctx = reg.run(40)
    reg.run(400)
    expect(rt.clock.tRenderS()).toBe(t)
    expect(ctx.tRenderS).toBe(t)
    expect(rt.interp.eMaxMs).toBeGreaterThanOrEqual(40)
    const out = newDronePoseSoA(2)
    expect(rt.sampleOne(1, t, out, 0)).toBe(true)
    const p = pos(t)
    const err = Math.hypot(out.pos[0] - p[0], out.pos[1] - p[1], out.pos[2] - p[2])
    expect(err).toBeLessThanOrEqual(0.01)
    expect(out.hold[0]).toBe(0)
    const q = quat(t)
    const dot = Math.abs(out.quat[0] * q[0] + out.quat[1] * q[1] + out.quat[2] * q[2] + out.quat[3] * q[3])
    expect((2 * Math.acos(Math.min(1, dot)) * 180) / Math.PI).toBeLessThan(0.1)
    rt.dispose()
  })
})
