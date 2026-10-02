// M12-AC-003 D_wall response (10 -> 20 Hz and jitter 0 -> 40 ms steps: 1 s smoothing, <= 10 %/s, 5 % hysteresis,
// clamp [60, 300] ms) and M12-AC-004 freeze and rate transitions (paused 500 ms: seen time = t_sim ± 1 ms; x1 <-> x10
// and resume ramps keep tRender monotone with speed >= 0.7 x rate; a new epoch restarts D from 0), plus the focus
// exception blend (FR-005).
import { describe, expect, it } from 'vitest'
import { DelayController, SimClockView, TIME_STATE as TS, WallDelay } from '@/engine/time/index'
import { lcg } from './helpers'

function feed(d: DelayController, from: number, to: number, hz: number, jitterMs: number, rnd: () => number, out?: { t: number; w: number }[]): number {
  let t = from
  let next = from
  while (t < to) {
    if (t >= next) {
      d.onSample(next + (jitterMs > 0 ? (rnd() * 2 - 1) * jitterMs : 0))
      next += 1000 / hz
    }
    d.tick(t, 1000 / 60, true, 1)
    out?.push({ t, w: d.dWallMs })
    t += 1000 / 60
  }
  return t
}

describe('D_wall (M12-AC-003)', () => {
  it('settles at 2/hz + jitter and moves smoothly, at most 10 %/s, after 10 -> 20 Hz and 0 -> 40 ms steps', () => {
    const d = new DelayController()
    const rnd = lcg(3)
    let t = feed(d, 0, 8000, 10, 0, rnd)
    expect(d.hzEff).toBeGreaterThan(9.5)
    expect(d.hzEff).toBeLessThan(10.5)
    expect(d.dWallMs).toBeGreaterThan(190)
    expect(d.dWallMs).toBeLessThan(215)
    const trace: { t: number; w: number }[] = []
    t = feed(d, t, t + 15_000, 20, 40, rnd, trace)
    for (let i = 1; i < trace.length; i++) {
      const dw = Math.abs(trace[i].w - trace[i - 1].w)
      const dt = (trace[i].t - trace[i - 1].t) / 1000
      expect(dw / trace[i - 1].w / dt).toBeLessThanOrEqual(0.1 + 1e-6)
    }
    const tgt = d.wall.target(d.hzEff, d.jitterMs)
    expect(tgt).toBeGreaterThan(100)
    expect(Math.abs(d.dWallMs - d.wall.held)).toBeLessThan(0.05 * d.wall.held + 1)
    expect(d.dWallMs).toBeGreaterThanOrEqual(60)
    expect(d.dWallMs).toBeLessThanOrEqual(300)
    void t
  })

  it('clamps to [60, 300] ms and ignores target changes within the 5 % hysteresis', () => {
    const lo = new DelayController()
    feed(lo, 0, 40_000, 60, 0, lcg(1))
    expect(lo.dWallMs).toBeGreaterThanOrEqual(60)
    expect(lo.dWallMs).toBeLessThan(62)
    const hi = new DelayController()
    feed(hi, 0, 5000, 4, 0, lcg(1))
    expect(hi.dWallMs).toBe(300)
    const w = new WallDelay()
    w.tick(16, 10, 0)
    expect(w.held).toBe(200)
    w.tick(16, 2000 / 206, 0) // +3 % target: inside the hysteresis
    expect(w.held).toBe(200)
    w.tick(16, 2000 / 212, 0) // +6 %: new held target, value follows at <= 10 %/s
    expect(w.held).toBeCloseTo(212, 6)
    expect(w.value - 200).toBeLessThanOrEqual(0.1 * 200 * 0.016 + 1e-9)
  })

  it('does not update hz_eff and jitter while frozen', () => {
    const d = new DelayController()
    feed(d, 0, 3000, 10, 0, lcg(1))
    const hz = d.hzEff
    d.tick(3000, 16, false, 1)
    for (let t = 3000; t < 6000; t += 500) d.onSample(t)
    expect(d.hzEff).toBe(hz)
  })
})

/** a server PLAYING at `rateAt(t)` from t = 0, TIME at 10 Hz and on every rate change, swarm samples at 10 Hz */
function drive(v: SimClockView, t0: number, t1: number, rateAt: (t: number) => number, state: (t: number) => number, sim: { t: number; last: number; rate: number }, out: { now: number; tr: number; rate: number }[]): void {
  for (let now = t0; now <= t1; now += 1000 / 60) {
    const r = state(now) === TS.PLAYING ? rateAt(now) : 0
    sim.t += (sim.rate || 0) * (1000 / 60)
    const changed = r !== sim.rate
    sim.rate = r
    if (changed || now - sim.last >= 100) {
      sim.last = now
      v.onTimeFields(state(now), 1, r || 1, sim.t, now, now)
    }
    if (Math.floor(now / 100) !== Math.floor((now - 1000 / 60) / 100)) v.delay.onSample(now)
    v.tick(now, now)
    out.push({ now, tr: v.tRenderS() * 1000, rate: r })
  }
}

describe('freeze and rate transitions (M12-AC-004)', () => {
  it('decays D to exactly 0 within --duration-quick after pause (seen time = t_sim)', () => {
    const v = new SimClockView()
    const out: { now: number; tr: number; rate: number }[] = []
    const sim = { t: 0, last: Number.NEGATIVE_INFINITY, rate: 0 }
    drive(v, 0, 6000, () => 10, (t) => (t < 5000 ? TS.PLAYING : TS.PAUSED), sim, out)
    const paused = out.filter((x) => x.now >= 5500)
    for (const x of paused) expect(Math.abs(x.tr - v.tSimMs)).toBeLessThanOrEqual(1)
    for (let i = 1; i < out.length; i++) expect(out[i].tr).toBeGreaterThanOrEqual(out[i - 1].tr - 1e-6)
  })

  it('x1 -> x10 and x10 -> x1 keep tRender monotone; the ramps keep the speed >= 0.7 x rate', () => {
    const v = new SimClockView()
    const out: { now: number; tr: number; rate: number }[] = []
    const sim = { t: 0, last: Number.NEGATIVE_INFINITY, rate: 0 }
    drive(v, 0, 12_000, (t) => (t < 4000 ? 1 : t < 8000 ? 10 : 1), () => TS.PLAYING, sim, out)
    for (let i = 1; i < out.length; i++) {
      const dt = out[i].now - out[i - 1].now
      const speed = (out[i].tr - out[i - 1].tr) / dt
      expect(speed).toBeGreaterThanOrEqual(0)
      if (out[i].now > 500 && Math.abs(out[i].now - 4000) > 40 && Math.abs(out[i].now - 8000) > 40) expect(speed).toBeGreaterThanOrEqual(0.7 * out[i].rate - 0.05)
    }
    // after the x10 ramp D_sim = 10 x D_wall
    const d = v.delay
    expect(Math.abs(d.dSimMs - 1 * d.dWallMs)).toBeLessThan(2)
  })

  it('resumes with a 1 s linear ramp from 0 and restarts from 0 on a new epoch', () => {
    const v = new SimClockView()
    const out: { now: number; tr: number; rate: number }[] = []
    const sim = { t: 0, last: Number.NEGATIVE_INFINITY, rate: 0 }
    drive(v, 0, 4500, () => 1, (t) => (t < 2000 || t >= 3000 ? TS.PLAYING : TS.PAUSED), sim, out)
    expect(v.delay.global.ramping).toBe(false)
    const resumed = out.filter((x) => x.now >= 3000 && x.now < 4000)
    for (let i = 1; i < resumed.length; i++) expect(resumed[i].tr).toBeGreaterThan(resumed[i - 1].tr)
    const dAfter = v.delay.dSimMs
    expect(dAfter).toBeGreaterThan(50)
    v.onTimeFields(TS.PLAYING, 2, 1, sim.t, 4500, 4500)
    expect(v.delay.dSimMs).toBe(0)
    v.tick(4516, 4516)
    expect(v.delay.dSimMs).toBeLessThan(dAfter * 0.1)
  })

  it('blends the focus delay in over 300 ms and exposes focusLowLatency', () => {
    const d = new DelayController()
    feed(d, 0, 3000, 10, 0, lcg(5))
    for (let t = 3000; t < 6000; t += 1000 / 60) {
      if (t < 3016) d.setFocus(true)
      d.onFocusSample(t)
      d.tick(t, 1000 / 60, true, 1)
    }
    expect(d.focusLowLatency).toBe(true)
    expect(d.focusBlend).toBe(1)
    expect(d.dFocusSimMs).toBeLessThan(d.dSimMs)
    expect(d.dFocusSimMs).toBeGreaterThanOrEqual(60)
    d.setFocus(false)
    d.tick(6000, 150, true, 1)
    expect(d.focusBlend).toBeCloseTo(0.5, 5)
  })

  it('follows the worker arrival statistics of the 60 Hz channel, not the main-thread frame rate (FX2-R3, D1-AC-26)', () => {
    // a 10 fps main thread ingests one focus sample per 100 ms frame; measured on the main thread that is 10 Hz with
    // frame jitter and D_focus clamps at 300 ms; the worker sees the 60 Hz channel (selHz 60, jitter p95 4 ms)
    const run = (worker: boolean): number => {
      const d = new DelayController()
      feed(d, 0, 3000, 10, 0, lcg(7))
      d.setFocus(true)
      const rnd = lcg(9)
      for (let t = 3000; t < 9000;) {
        const dt = rnd() < 0.3 ? 233 : 100
        if (worker) d.onFocusSample(t, 60, 4)
        else d.onFocusSample(t)
        d.tick(t, dt, true, 1)
        t += dt
      }
      return d.dFocusSimMs
    }
    expect(run(false)).toBeGreaterThan(250)
    const w = run(true)
    expect(w).toBeGreaterThanOrEqual(60)
    expect(w).toBeLessThan(65)
    // unknown worker statistics (NaN jitter, no 60 Hz channel) fall back to the main-thread arrival times
    const d = new DelayController()
    d.setFocus(true)
    d.onFocusSample(0, 0, Number.NaN)
    d.onFocusSample(100, 60, Number.NaN)
    expect(d.focusStats.hzEff).toBeGreaterThan(9)
    expect(d.focusStats.hzEff).toBeLessThan(11)
  })
})
