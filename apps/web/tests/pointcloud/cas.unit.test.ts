// M05-AC-011 (AWR-18 §4.3 item 3): CAS unit cases - freezes, backlog semantics, bumpless rung moves, upDelay doubling,
// quality floor and PerfGovernor override, degenerate rungs, saturation durations, B reversals.
import { describe, expect, it } from 'vitest'
import { CascadeController, FREEZE_EXTERNAL, FREEZE_SHADER_COMPILE, FREEZE_WARMUP, type CasOptions } from '@/engine/pointcloud/core/CascadeController'
import { casFreezeMask } from '@/engine/pointcloud/core/stats'
import { LADDER, PC, deviceParams } from '@/engine/pointcloud/params'

const POOL_S = PC.poolWidth * PC.poolRowsS
const POOL_I = PC.poolWidth * PC.poolRowsIgpu
const POOL_D = PC.poolWidth * PC.poolRowsDgpu

function software(extra: Partial<CasOptions> = {}): CascadeController {
  return new CascadeController({ startIndex: 0, floorIndex: 0, ceilIndex: 1, targetMs: 33.3, tailK: 2, initialB: 25_000, Bfloor: 20_000,
    poolCapacityPts: POOL_S, rsLock: 0.5, ...extra })
}
function igpu(extra: Partial<CasOptions> = {}): CascadeController {
  return new CascadeController({ startIndex: 3, floorIndex: 2, ceilIndex: 4, targetMs: 16.7, tailK: 1.6, Bfloor: LADDER[2].lo, poolCapacityPts: POOL_I, ...extra })
}

/** feed a constant interval until cond() or maxMs; returns the time reached */
function feedUntil(c: CascadeController, t0: number, maxMs: number, dt: number, cond: () => boolean, o: { pending?: boolean } = {}): number {
  let t = t0
  while (t < t0 + maxMs && !cond()) {
    t += dt
    c.sample(dt, t, 0, o.pending ?? false)
  }
  return t
}

/** feed a constant interval for ms milliseconds; returns the time reached */
function feed(c: CascadeController, t0: number, ms: number, dt: number, o: { freeze?: number; pending?: boolean; workMs?: number } = {}): number {
  let t = t0
  while (t < t0 + ms) {
    t += dt
    c.sample(dt, t, o.freeze ?? 0, o.pending ?? false, o.workMs)
  }
  return t
}

describe('freeze conditions (M05-FR-040)', () => {
  it('maps the five conditions onto the mask', () => {
    expect(casFreezeMask({ frozen: false, hidden: true, compiled: false, warmupLeft: 0 })).toBe(FREEZE_EXTERNAL) // page hidden
    expect(casFreezeMask({ frozen: true, hidden: false, compiled: false, warmupLeft: 0 })).toBe(FREEZE_EXTERNAL) // modal throttled or user cap
    expect(casFreezeMask({ frozen: false, hidden: false, compiled: false, warmupLeft: 30 })).toBe(FREEZE_WARMUP) // reveal / world switch
    expect(casFreezeMask({ frozen: false, hidden: false, compiled: true, warmupLeft: 0 })).toBe(FREEZE_SHADER_COMPILE) // programs grew
    expect(casFreezeMask({ frozen: false, hidden: false, compiled: false, warmupLeft: 0 })).toBe(0)
  })
  it.each([FREEZE_EXTERNAL, FREEZE_SHADER_COMPILE, FREEZE_WARMUP, FREEZE_EXTERNAL | FREEZE_WARMUP])('mask %i: no evaluation, window cleared', (mask) => {
    const c = software()
    feed(c, 0, 2000, 50, { freeze: mask })
    expect(c.evals).toBe(0)
    expect(c.frozenFrames).toBe(40)
    expect(c.B).toBe(25_000)
    // after the freeze the window starts empty: 6 samples are needed before the first evaluation
    let t = 2000
    for (let i = 0; i < 5; i++) c.sample(50, (t += 50), 0, false)
    expect(c.evals).toBe(0)
    c.sample(50, (t += 50), 0, false)
    expect(c.evals).toBe(1)
  })
})

describe('inner loop (M05-FR-038, FR-039)', () => {
  it('overload shrinks B by (1/r)^0.8 clamped to [0.5, 0.92]', () => {
    const c = software({ initialB: 40_000 })
    feed(c, 0, 300, 50) // r = 1.5
    expect(c.evals).toBe(1)
    expect(c.B).toBeCloseTo(40_000 * Math.min(Math.max((1 / (50 / 33.3)) ** 0.8, 0.5), 0.92), 3)
  })
  it('two evaluations with r < 0.85 grow B by 8 % without a backlog, never with one', () => {
    const a = software({ initialB: 25_000 })
    const b = software({ initialB: 25_000 })
    feed(a, 0, 510, 16.7)
    feed(b, 0, 510, 16.7, { pending: true })
    expect(a.evals).toBe(2)
    expect(a.B).toBeCloseTo(25_000 * 1.08, 6)
    expect(b.evals).toBe(2)
    expect(b.B).toBe(25_000)
  })
  it('on target it probes +3 % every 8 evaluations, also with a backlog (ADR-012)', () => {
    const c = software({ initialB: 25_000 })
    feedUntil(c, 0, 5000, 33.3, () => c.evals >= 8, { pending: true })
    expect(c.evals).toBe(8)
    expect(c.B).toBeCloseTo(25_000 * 1.03, 6)
  })
  it('a heavy tail shrinks B by 0.9', () => {
    const c = software({ initialB: 30_000 })
    let t = 0
    for (let i = 0; i < 24; i++) c.sample(i % 5 === 0 ? 80 : 33.3, (t += 33.3), 0, false) // p50 on target, p90 > 2 T
    expect(c.evals).toBeGreaterThan(0)
    expect(c.B).toBeLessThan(30_000)
  })
  it('counts B reversals (adjacent effective changes > 2 % with opposite signs)', () => {
    const c = software({ initialB: 30_000 })
    let t = feedUntil(c, 0, 2000, 50, () => c.B < 30_000) // down
    expect(c.reversals).toBe(0)
    const b1 = c.B
    t = feedUntil(c, t, 5000, 16.7, () => c.B > b1) // up (x1.08 after two evaluations)
    expect(c.reversals).toBe(1)
    const b2 = c.B
    feedUntil(c, t, 5000, 60, () => c.B < b2) // down
    expect(c.reversals).toBe(2)
  })
})

describe('outer loop (M05-FR-041, FR-042)', () => {
  it('down move takes the new rung hi, up move the new lo', () => {
    // software soft rung, overloaded at its lower edge -> soft-min with B = 40k
    const c = software({ startIndex: 1, initialB: 40_000 })
    feedUntil(c, 0, 4000, 100, () => c.index === 0)
    expect(c.index).toBe(0)
    expect(c.B).toBe(40_000)
    expect(c.rungChanges).toBe(1)
    // hardware iGPU from low (3) at the ceiling with headroom -> medium (4) at lo = 1.5M
    const h = igpu({ initialB: 1_500_000 })
    feedUntil(h, 0, 20_000, 8, () => h.index === 4)
    expect(h.index).toBe(4)
    expect(h.B).toBe(1_500_000)
  })
  it('upDelay doubles when a down move follows an up move within 10 s, capped at 120 s', () => {
    const h = igpu({ initialB: 1_500_000 })
    let t = feedUntil(h, 0, 20_000, 8, () => h.index === 4) // up to 4
    expect(h.index).toBe(4)
    const up1 = t
    t = feedUntil(h, t, 9000, 60, () => h.index === 3) // heavy overload: down within 10 s of the up move
    expect(h.index).toBe(3)
    expect(h.B).toBe(LADDER[3].hi)
    expect(t - up1).toBeLessThan(10_000)
    expect((h as unknown as { upDelay: number }).upDelay).toBe(10_000)
    for (let k = 0; k < 8; k++) (h as unknown as { upDelay: number }).upDelay = Math.min((h as unknown as { upDelay: number }).upDelay * 2, PC.casUpDelayMaxMs)
    expect((h as unknown as { upDelay: number }).upDelay).toBe(120_000)
  })
  it('the quality floor holds B: no down move, atFloorSinceMs grows (software, rung 0)', () => {
    const c = software()
    const t = feed(c, 0, 5000, 100)
    expect(c.index).toBe(0)
    expect(c.B).toBe(20_000)
    expect(c.floorHeld).toBe(true)
    expect(c.state(t).atFloorSinceMs).toBeGreaterThan(3000)
  })
  it('hardware stops at the lowest allowed rung until the override', () => {
    const h = igpu({ startIndex: 2, initialB: LADDER[2].lo })
    let t = feed(h, 0, 5000, 60)
    expect(h.index).toBe(2)
    expect(h.state(t).atFloorSinceMs).toBeGreaterThan(2000)
    h.setFloorOverride(LADDER[0].lo)
    t = feed(h, t, 8000, 60)
    expect(h.index).toBe(0)
    h.setFloorOverride(null)
    expect(h.floorIndex).toBe(2)
  })
  it('setFloorOverride(10000) lets software B reach 10k', () => {
    const c = software()
    let t = feed(c, 0, 3000, 100)
    expect(c.B).toBe(20_000)
    c.setFloorOverride(10_000)
    t = feed(c, t, 3000, 100)
    expect(c.B).toBe(10_000)
    expect(c.state(t).Bfloor).toBe(10_000)
  })
  it('atFloorTotalMs is monotone and does not grow while frozen', () => {
    const c = software()
    let t = feed(c, 0, 3000, 100)
    const a = c.state(t).atFloorTotalMs
    expect(a).toBeGreaterThan(1000)
    t = feed(c, t, 3000, 100, { freeze: FREEZE_EXTERNAL })
    expect(c.state(t).atFloorTotalMs).toBe(a)
    expect(c.state(t).atFloorSinceMs).toBe(0)
    t = feed(c, t, 3000, 100)
    expect(c.state(t).atFloorTotalMs).toBeGreaterThan(a)
  })
})

describe('capacity clamp and degenerate rungs (M05 §6.8.4)', () => {
  it('Tier S: soft-min band [20k, 40k], soft [40k, 150k]; no degenerate rung below the ceiling', () => {
    const c = software()
    expect([c.lo, c.hi]).toEqual([20_000, 40_000])
    c.setManual(1)
    expect([c.lo, c.hi]).toEqual([40_000, 150_000])
    expect(c.isDegenerate(1)).toBe(false)
  })
  it('iGPU: rungs 5 and 6 are degenerate; start rung 5 falls back to 4; auto mode never moves into 5', () => {
    const h = igpu({ startIndex: 5 })
    expect(h.index).toBe(4)
    expect(h.isDegenerate(5)).toBe(true)
    expect(h.isDegenerate(6)).toBe(true)
    expect(h.isDegenerate(4)).toBe(false)
    const t = feed(h, 0, 20_000, 8)
    expect(h.index).toBe(4)
    expect(h.state(t).atCeilSinceMs).toBeGreaterThan(10_000)
  })
  it('dGPU: only rung 6 is degenerate; device parameters follow ADR-044', () => {
    const d = new CascadeController({ startIndex: 5, floorIndex: 2, ceilIndex: 5, targetMs: 16.7, tailK: 1.6, Bfloor: LADDER[2].lo, poolCapacityPts: POOL_D })
    expect(d.index).toBe(5)
    expect(d.isDegenerate(6)).toBe(true)
    expect(d.isDegenerate(5)).toBe(false)
    const p = deviceParams('B', 'iGPU', 3, 2)
    expect([p.floorIndex, p.ceilIndex, p.b0, p.bFloor]).toEqual([2, 4, 1_500_000, 150_000])
    const s = deviceParams('S', 'software', 0, 0)
    expect([s.floorIndex, s.ceilIndex, s.b0, s.bFloor, s.uploadPtsPerFrame, s.inflight, s.poolRows]).toEqual([0, 1, 25_000, 20_000, 20_000, 4, 62])
  })
  it('manual ultra on the iGPU pool: B pinned at 0.6 x capacity and clampedByCapacity', () => {
    const h = igpu()
    h.setManual(6)
    const cap = 0.6 * POOL_I
    expect(h.manual).toBe(true)
    expect(h.B).toBe(cap)
    expect(h.clampedByCapacity).toBe(true)
    feed(h, 0, 5000, 30)
    expect(h.B).toBe(cap)
    expect(h.index).toBe(6)
    expect(h.rungChanges).toBe(0) // manual moves are not counted (M05-FR-044)
    h.setManual(null)
    feed(h, 5000, 5000, 60)
    expect(h.index).toBeLessThan(6)
  })
  it('state() is one preallocated object with rs locked at 0.5 on software', () => {
    const c = software()
    const a = c.state(0)
    c.setManual(1)
    const b = c.state(0)
    expect(a).toBe(b)
    expect(b.rs).toBe(0.5)
    expect(igpu().state(0).rs).toBe(0.75)
  })
})
