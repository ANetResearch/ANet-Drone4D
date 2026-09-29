// M12-AC-001 TIME decoding against golden/rt/time.json (10 states x bit 7 x reserved bits, u16 epoch wrap, unknown
// state -> STALLED); M12-AC-002 simNow discipline (±30 ms network jitter, 100 ms TIME drops, 1 s stop): monotone,
// p95 error <= 5 ms x rate against the server truth, STALE after 1 s, capped at +rate x 1 s; FR-013 step hold and glide.
import { describe, expect, it } from 'vitest'
import { SimClockView, onEpoch, perfTime, TIME_STATE as TS } from '@/engine/time/index'
import { readJson } from '../contracts/util'
import { lcg } from './helpers'

interface Case { hex: string; state: number; replay: boolean; reserved_bits: number; epoch: number; rate: number; t_sim_ns: number; t_srv_ns: number; advances: boolean }
const cases = readJson<{ cases: Case[] }>('golden/rt/time.json').cases

describe('TIME semantics (M12-AC-001)', () => {
  it('masks bit 7 and the reserved bits, advances only in PLAYING and LIVE, for all 40 golden cases', () => {
    expect(cases.length).toBe(40)
    for (const c of cases) {
      const stateByte = parseInt(c.hex.slice(2, 4), 16)
      const v = new SimClockView()
      v.onTimeFields(stateByte, c.epoch, c.rate, c.t_sim_ns / 1e6, c.t_srv_ns / 1e6, 0)
      expect(v.state4, c.hex).toBe(c.state)
      expect(v.replay, c.hex).toBe(c.replay)
      expect(v.advancing, c.hex).toBe(c.advances)
      expect(v.epoch).toBe(c.epoch)
      v.tick(0, c.t_srv_ns / 1e6 + 100)
      const want = c.advances ? c.t_sim_ns / 1e6 + c.rate * 100 : c.t_sim_ns / 1e6
      expect(v.simNowS() * 1000).toBeCloseTo(want, 3)
    }
  })

  it('treats unknown state values as STALLED and counts them', () => {
    const v = new SimClockView()
    const before = perfTime.unknownState
    v.onTimeFields(0x0c, 1, 1, 5000, 0, 0)
    expect(v.state4).toBe(TS.STALLED)
    expect(v.advancing).toBe(false)
    expect(v.unknownState).toBe(1)
    expect(perfTime.unknownState).toBeGreaterThanOrEqual(1)
    expect(before).toBeGreaterThanOrEqual(0)
  })

  it('compares epochs for equality only: 65535 -> 0 is a new epoch and fires onEpoch', () => {
    const seen: number[] = []
    const off = onEpoch((e) => seen.push(e))
    const v = new SimClockView()
    v.onTimeFields(9, 65535, 1, 0, 0, 0)
    v.onTimeFields(9, 65535, 1, 10, 10, 0)
    v.onTimeFields(9, 0, 1, 20, 20, 0)
    off()
    expect(seen).toEqual([65535, 0])
  })
})

describe('simNow discipline (M12-AC-002)', () => {
  function run(rate: number, seed: number) {
    const rnd = lcg(seed)
    const v = new SimClockView()
    // server: PLAYING at `rate` from t_srv = 0; TIME every 100 ms (server clock), 10 % of them lost, delay 20 ± 30 ms
    const sends: { at: number; tSrv: number }[] = []
    for (let t = 0; t <= 20_000; t += 100) if (rnd() > 0.1 || t === 0) sends.push({ at: t + 20 + (rnd() * 2 - 1) * 30, tSrv: t })
    sends.sort((a, b) => a.at - b.at)
    let k = 0
    let prev = Number.NEGATIVE_INFINITY
    const err: number[] = []
    for (let now = 0; now <= 20_000; now += 1000 / 60) {
      while (k < sends.length && sends[k].at <= now) {
        const s = sends[k++]
        v.onTimeFields(TS.PLAYING, 1, rate, rate * s.tSrv, s.tSrv, s.at)
      }
      // offset estimate error ±2 ms
      v.tick(now, now + (rnd() * 2 - 1) * 2)
      const sim = v.simNowS() * 1000
      expect(sim).toBeGreaterThanOrEqual(prev)
      prev = sim
      if (now > 500) err.push(Math.abs(sim - rate * now))
    }
    err.sort((a, b) => a - b)
    return err[Math.floor(0.95 * err.length)]
  }
  it('is monotone and within 5 ms x rate (p95) of the server at x1 and x10', () => {
    expect(run(1, 7)).toBeLessThanOrEqual(5)
    expect(run(10, 11)).toBeLessThanOrEqual(50)
  })

  it('caps extrapolation at rate x 1 s and turns STALE after 1 s without TIME', () => {
    const v = new SimClockView()
    v.onTimeFields(TS.LIVE, 1, 1, 1000, 50_000, 0)
    v.tick(0, 50_000)
    expect(v.simNowS()).toBeCloseTo(1, 6)
    v.tick(100, 50_100)
    expect(v.simNowS()).toBeCloseTo(1.1, 3)
    expect(v.stale).toBe(false)
    v.tick(3000, 53_000)
    expect(v.simNowS()).toBeLessThanOrEqual(2 + 1e-9)
    expect(v.stale).toBe(true)
  })

  it('snaps on state changes and freezes at TIME.t_sim; the seen time equals t_sim once D has decayed', () => {
    const v = new SimClockView()
    v.onTimeFields(TS.PLAYING, 1, 1, 0, 0, 0)
    for (let t = 0; t <= 3000; t += 16) {
      v.delay.onSample(t - (t % 100))
      v.tick(t, t)
    }
    expect(v.dGlobalMs).toBeGreaterThan(50)
    v.onTimeFields(TS.PAUSED, 1, 1, 3000, 3000, 3000)
    v.tick(3016, 3016)
    expect(v.simNowS()).toBe(3)
    v.tick(3500, 3500)
    expect(v.tRenderS()).toBe(3)
  })
})

describe('single step (FR-013)', () => {
  it('holds while STEPPING and glides to the new t_sim over --duration-fast after PAUSED', () => {
    const v = new SimClockView()
    v.onTimeFields(TS.PAUSED, 1, 1, 1000, 1000, 0)
    v.tick(0, 0)
    v.tick(400, 400)
    expect(v.tRenderS()).toBe(1)
    v.onTimeFields(TS.STEPPING, 1, 1, 1040, 1000, 400)
    v.tick(420, 420)
    expect(v.tRenderS()).toBe(1)
    v.onTimeFields(TS.PAUSED, 1, 1, 1100, 1000, 440)
    v.tick(450, 450)
    const a = v.tRenderS()
    v.tick(550, 550)
    const b = v.tRenderS()
    expect(a).toBeGreaterThanOrEqual(1)
    expect(b).toBeGreaterThan(a)
    expect(b).toBeLessThan(1.1)
    v.tick(720, 720)
    expect(v.tRenderS()).toBe(1.1)
    expect(v.simNowS()).toBe(1.1)
  })

  it('jumps without a glide in reduced motion and for steps above 1 s', () => {
    const v = new SimClockView()
    v.reduced = true
    v.onTimeFields(TS.PAUSED, 1, 1, 1000, 0, 0)
    v.tick(0, 0)
    v.onTimeFields(TS.PAUSED, 1, 1, 1100, 0, 10)
    v.tick(20, 20)
    expect(v.tRenderS()).toBe(1.1)
    const w = new SimClockView()
    w.onTimeFields(TS.PAUSED, 1, 1, 1000, 0, 0)
    w.tick(0, 0)
    w.onTimeFields(TS.PAUSED, 1, 1, 3000, 0, 10)
    w.tick(20, 20)
    expect(w.tRenderS()).toBe(3)
  })
})
