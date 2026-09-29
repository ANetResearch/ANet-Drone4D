// M06-AC-053 (M06 §6.18; PRD-FR-014): stores/perf.ts summary equals the __perf values (p50/p95 of the last 2 s, budget,
// progress, governor step and label, latency p95) and writes only when something changed (<= 4 Hz on Tier S through
// the governor-phase task registration).
import { describe, expect, it } from 'vitest'
import { perfProbe, pushRing, registered } from '@/engine'
import { perfStore, quantile, summarisePerf } from '@/stores/perf'

describe('stores/perf summary (M06-AC-053)', () => {
  it('quantiles over the last 2 s and change-only writes', () => {
    const p = perfProbe()
    p.frame.interval.n = 0
    for (let i = 0; i < 200; i++) pushRing(p.frame.interval, i < 190 ? 33.3 : 100)
    p.pc.drawn = 24_000
    p.pc.progress = 0.956
    for (let i = 0; i < 50; i++) pushRing(p.latency.tSimToPixelMs, 90 + i)
    summarisePerf()
    const s = perfStore.getState()
    expect(s.p50Ms).toBeCloseTo(33.3, 1)
    expect(s.p95Ms).toBe(100)
    expect(s.B).toBe(24_000)
    expect(s.progress).toBe(0.96)
    expect(s.latencyP95Ms).toBe(137)
    let writes = 0
    const off = perfStore.subscribe(() => writes++)
    summarisePerf()
    summarisePerf()
    expect(writes).toBe(0) // unchanged -> no write
    p.pc.drawn = 25_000
    summarisePerf()
    expect(writes).toBe(1)
    off()
    expect(registered('governor')).toEqual(expect.arrayContaining(['perf-summary.s', 'perf-summary.ba']))
    const a = new Float64Array([5, 1, 4, 2, 3])
    expect(quantile(a, 5, 0.5)).toBe(3)
  })
})
