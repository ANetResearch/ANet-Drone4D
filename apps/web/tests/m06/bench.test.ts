// Bench helpers (M06-FR-080; AWR-18 §5.2): the pair median over a ring, the AWR-18 §5.1 layer groups.
import { describe, expect, it } from 'vitest'
import { pushRing, ring } from '@/engine'
import { BENCH, PAIR_GROUPS, ringMedian } from '@/viewport/bench'

describe('layers pair statistics', () => {
  it('median of the stored deltas (odd, even, wrapped ring)', () => {
    const s = new Float64Array(BENCH.ringCap)
    const r = ring(8)
    expect(Number.isNaN(ringMedian(r, s))).toBe(true)
    for (const v of [5, 1, 3]) pushRing(r, v)
    expect(ringMedian(r, s)).toBe(3)
    pushRing(r, 10)
    expect(ringMedian(r, s)).toBe(4)
    for (let i = 0; i < 20; i++) pushRing(r, 100 + i) // wraps: the last 8 values 112..119
    expect(ringMedian(r, s)).toBe(115.5)
  })
  it('groups follow AWR-18 §5.1', () => {
    expect(PAIR_GROUPS.map((g) => g[0])).toEqual(['drones', 'trails', 'environment', 'groundSky'])
    expect(PAIR_GROUPS[1][1]).toEqual(['trails', 'frustums', 'mission', 'zones', 'glyphs'])
  })
})
