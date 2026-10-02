// hypot2/3/4 repeat V8's Math.hypot algorithm without its argument array (FX2-R2, D1-AC-30): results are bit-identical
// to Math.hypot on random, tiny, huge and special inputs, so the selector stays equal to the g02 oracle.
import { describe, expect, it } from 'vitest'
import { hypot2, hypot3, hypot4 } from '@/engine/hypot'

describe('allocation-free hypot', () => {
  it('is bit-identical to Math.hypot', () => {
    let seed = 1
    const rnd = (): number => ((seed = (seed * 1103515245 + 12345) >>> 0) / 4294967296)
    const pick = (): number => {
      const r = rnd()
      if (r < 0.05) return 0
      if (r < 0.1) return -0
      const mag = 10 ** (rnd() * 40 - 20)
      return (rnd() < 0.5 ? -1 : 1) * mag * rnd()
    }
    let bad = 0
    for (let i = 0; i < 200_000; i++) {
      const a = pick()
      const b = pick()
      const c = pick()
      const d = pick()
      if (!Object.is(hypot2(a, b), Math.hypot(a, b))) bad++
      if (!Object.is(hypot3(a, b, c), Math.hypot(a, b, c))) bad++
      if (!Object.is(hypot4(a, b, c, d), Math.hypot(a, b, c, d))) bad++
    }
    expect(bad).toBe(0)
    for (const s of [Number.NaN, Infinity, -Infinity, 0, -0, 1e308, 5e-324]) {
      expect(Object.is(hypot3(s, 1, 2), Math.hypot(s, 1, 2))).toBe(true)
      expect(Object.is(hypot3(Number.NaN, Infinity, 1), Math.hypot(Number.NaN, Infinity, 1))).toBe(true)
      expect(Object.is(hypot2(s, -s), Math.hypot(s, -s))).toBe(true)
      expect(Object.is(hypot4(1, s, 2, 3), Math.hypot(1, s, 2, 3))).toBe(true)
    }
  })
})
