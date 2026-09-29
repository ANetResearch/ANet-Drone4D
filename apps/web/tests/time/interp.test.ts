// M12-AC-009 ring writes (100k samples, Lite32 and Full64 mixed, 5 % out of order: strictly increasing times, Full64
// wins at equal times, capacity grows only for unseen agents), M12-AC-010 Hermite accuracy against the Python oracle
// (tools/bench/rec/interp_error.py: <= 1e-4 m from the oracle, <= 1 mm from the truth at h = 0.1 s), M12-AC-011
// extrapolation and HOLD, attitude slerp and ω integration, clamping, clearRange, sampleOne.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { InterpRing, hermite3, integrateOmegaInto, newDronePoseSoA, slerpInto } from '@/engine/time/index'
import { lcg } from './helpers'

interface GoldenCase { name: string; h: number; t: number[]; p: number[][]; v: number[][]; tq: number[]; hermite: number[][]; truth: number[][] }
const golden = JSON.parse(readFileSync(new URL('./fixtures/hermite_golden.json', import.meta.url), 'utf8')) as { cases: GoldenCase[] }

describe('Hermite against the Python oracle (M12-AC-010)', () => {
  it.each(golden.cases.map((c) => [c.name, c] as const))('%s at h = 0.1 s', (_n, c) => {
    const out = new Float64Array(3)
    let worstOracle = 0
    let worstTruth = 0
    for (let q = 0; q < c.tq.length; q++) {
      const t = c.tq[q]
      let k = 0
      while (k < c.t.length - 2 && c.t[k + 1] <= t) k++
      const s = (t - c.t[k]) / c.h
      hermite3([...c.p[k], 0], [...c.v[k], 0], [...c.p[k + 1], 0], [...c.v[k + 1], 0], c.h, s, out, 0)
      worstOracle = Math.max(worstOracle, Math.hypot(out[0] - c.hermite[q][0], out[1] - c.hermite[q][1]))
      worstTruth = Math.max(worstTruth, Math.hypot(out[0] - c.truth[q][0], out[1] - c.truth[q][1]))
    }
    expect(worstOracle).toBeLessThanOrEqual(1e-4)
    expect(worstTruth).toBeLessThanOrEqual(1e-3)
  })

  it('the ring reproduces the oracle through float32 storage (turn, 10 Hz)', () => {
    const c = golden.cases[0]
    const ring = new InterpRing()
    const s = ring.slotFor(3)
    for (let k = 0; k < c.t.length; k++) ring.push(s, c.t[k] * 1000, c.p[k][0], c.p[k][1], 50, c.v[k][0], c.v[k][1], 0, 0, 0, 0, 1, 0)
    const out = newDronePoseSoA(4)
    let worst = 0
    for (let q = 0; q < c.tq.length; q++) {
      if (c.tq[q] < c.t[c.t.length - 32 + 1]) continue // older than the ring (K = 32)
      ring.sampleSwarm(c.tq[q], out)
      worst = Math.max(worst, Math.hypot(out.pos[0] - c.hermite[q][0], out.pos[1] - c.hermite[q][1]))
      expect(out.hold[0]).toBe(0)
    }
    expect(worst).toBeLessThanOrEqual(1e-4)
  })
})

describe('ring writes (M12-AC-009)', () => {
  it('keeps times strictly increasing over 100k mixed samples with 5 % out of order; Full64 wins at equal times', () => {
    const rnd = lcg(42)
    const ring = new InterpRing()
    const s = ring.slotFor(9)
    let t = 0
    let pushed = 0
    let lastSrcAtT = new Map<number, number>()
    for (let i = 0; i < 100_000; i++) {
      t += 8
      let ts = t
      if (rnd() < 0.05) ts = t - Math.floor(rnd() * 20) * 8 // out of order, possibly older than the ring
      const src = rnd() < 0.5 ? 1 : 0
      ring.push(s, ts, ts / 100, 0, 0, 0.01, 0, 0, 0, 0, 0, 1, src)
      pushed++
      if (src === 1 || !lastSrcAtT.has(ts)) lastSrcAtT.set(ts, Math.max(src, lastSrcAtT.get(ts) ?? 0))
      if (i % 97 === 0) {
        const times = ring.timesOf(9)
        for (let k = 1; k < times.length; k++) expect(times[k]).toBeGreaterThan(times[k - 1])
        expect(times.length).toBe(Math.min(32, times.length))
      }
      if (lastSrcAtT.size > 2000) lastSrcAtT = new Map()
    }
    expect(pushed).toBe(100_000)
    // equal time: Lite32 after Full64 does not replace it
    const r2 = new InterpRing()
    const a = r2.slotFor(1)
    r2.push(a, 100, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1)
    r2.push(a, 100, 2, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)
    expect(r2.p[3 * (a * 32 + r2.head[a])]).toBe(1)
    r2.push(a, 100, 3, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1)
    expect(r2.p[3 * (a * 32 + r2.head[a])]).toBe(3)
    // inserted in the middle (not full), and when full the oldest drops
    r2.push(a, 300, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)
    r2.push(a, 200, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)
    expect(r2.timesOf(1)).toEqual([100, 200, 300])
    for (let k = 4; k <= 40; k++) r2.push(a, k * 100, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)
    expect(r2.timesOf(1).length).toBe(32)
    r2.push(a, 3050, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)
    const tt = r2.timesOf(1)
    expect(tt.length).toBe(32)
    expect(tt.includes(3050)).toBe(true)
    expect(tt[0]).toBe(1000)
    r2.push(a, 50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0) // older than the oldest: dropped
    expect(r2.timesOf(1)[0]).toBe(1000)
  })

  it('grows capacity in steps of 256 only for unseen agents', () => {
    const ring = new InterpRing()
    expect(ring.cap).toBe(256)
    for (let a = 0; a < 300; a++) ring.slotFor(a)
    expect(ring.cap).toBe(512)
    const t = ring.t
    ring.push(ring.slotFor(5), 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)
    expect(ring.t).toBe(t)
  })
})

describe('extrapolation and HOLD (M12-AC-011)', () => {
  it('extrapolates linearly up to E_max = 3 / hz, then holds; a new sample releases HOLD at once', () => {
    const ring = new InterpRing()
    const s = ring.slotFor(1)
    for (let k = 0; k <= 10; k++) ring.push(s, k * 100, k * 0.2, 0, 10, 2, 0, 0, 0, 0, 0, 1, 0)
    ring.eMaxMs = 300
    const out = newDronePoseSoA(2)
    ring.sampleSwarm(1.2, out)
    expect(out.pos[0]).toBeCloseTo(2.4, 5)
    expect(out.hold[0]).toBe(0)
    ring.sampleSwarm(2.0, out)
    expect(out.pos[0]).toBeCloseTo(2.6, 5)
    expect(out.hold[0]).toBe(1)
    expect(out.ageS[0]).toBeCloseTo(1, 6)
    expect(ring.lastHold).toBe(1)
    ring.push(s, 2000, 4, 0, 10, 2, 0, 0, 0, 0, 0, 1, 0)
    ring.sampleSwarm(2.0, out)
    expect(out.hold[0]).toBe(0)
    expect(out.pos[0]).toBeCloseTo(4, 5)
  })

  it('clamps before the oldest sample', () => {
    const ring = new InterpRing()
    const s = ring.slotFor(1)
    ring.push(s, 1000, 5, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0)
    ring.push(s, 1100, 5.1, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0)
    const out = newDronePoseSoA(1)
    ring.sampleSwarm(0.5, out)
    expect(out.clamped[0]).toBe(1)
    expect(out.pos[0]).toBe(5)
  })

  it('integrates the body rate ω of Full64 samples while extrapolating the attitude', () => {
    const ring = new InterpRing()
    const s = ring.slotFor(1)
    ring.push(s, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1) // yaw rate 1 rad/s
    ring.eMaxMs = 1000
    const out = newDronePoseSoA(1)
    ring.sampleSwarm(0.5, out)
    const yaw = 2 * Math.atan2(out.quat[2], out.quat[3])
    expect(yaw).toBeCloseTo(0.5, 5)
  })
})

describe('attitude kernels', () => {
  it('slerp takes the short arc and falls back to nlerp near identity', () => {
    const a = new Float32Array([0, 0, 0, 1])
    const b = new Float32Array([0, 0, -Math.sin(0.25), -Math.cos(0.25)]) // same rotation as +0.5 rad yaw, negated
    const out = new Float32Array(4)
    slerpInto(a, 0, b, 0, 0.5, out, 0)
    expect(2 * Math.atan2(out[2], out[3])).toBeCloseTo(0.25, 5)
    const c = new Float32Array([0, 0, Math.sin(1e-4), Math.cos(1e-4)])
    slerpInto(a, 0, c, 0, 0.5, out, 0)
    expect(Math.hypot(out[0], out[1], out[2], out[3])).toBeCloseTo(1, 6)
  })
  it('integrateOmegaInto rotates about the body axis', () => {
    const q = new Float32Array([0, 0, Math.sin(Math.PI / 4), Math.cos(Math.PI / 4)]) // yaw 90 deg
    const w = new Float32Array([1, 0, 0]) // roll rate in the body frame
    const out = new Float32Array(4)
    integrateOmegaInto(q, 0, w, 0, 0.3, out, 0)
    expect(Math.hypot(out[0], out[1], out[2], out[3])).toBeCloseTo(1, 6)
    expect(Math.abs(out[0])).toBeGreaterThan(0.05)
  })
})

describe('ranges and single vehicles', () => {
  it('clearRange only empties agents of that producer range; sampleOne reads one vehicle', () => {
    const ring = new InterpRing()
    for (const a of [1, 2, 300, 301]) ring.push(ring.slotFor(a), 0, a, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)
    ring.clearRange(256, 256)
    const out = newDronePoseSoA(8)
    ring.sampleSwarm(0, out)
    expect(Array.from(out.agentNo.subarray(0, out.n)).sort((x, y) => x - y)).toEqual([1, 2])
    expect(ring.sampleOne(300, 0, out, 0)).toBe(false)
    expect(ring.sampleOne(2, 0, out, 3)).toBe(true)
    expect(out.pos[9]).toBe(2)
    ring.clearAll()
    ring.sampleSwarm(0, out)
    expect(out.n).toBe(0)
  })
})
