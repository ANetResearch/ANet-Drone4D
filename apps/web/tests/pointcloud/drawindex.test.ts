// DrawTable block index (FX2-R2; M05 §6.7.2): for every vertex v of a frame, the entry found by the bounded search inside
// [block[v >> 6], block[(v >> 6) + 1]] equals the binary search over the whole prefix column (largest prefixStart <= v),
// including zero-length entries, single entries, entries shorter than a block and the last block; the search never needs
// more than log2(DRAW_BLOCK + 1) + 1 steps.
import { describe, expect, it } from 'vitest'
import { DRAW_BLOCK, buildBlockIndex } from '@/engine/pointcloud/gpu/DrawTable'
import { PC } from '@/engine/pointcloud/params'

/** DrawTable words (x = prefixStart) for the given entry counts */
function table(counts: readonly number[]): { entries: Uint32Array; drawn: number } {
  const entries = new Uint32Array(4 * Math.max(1, counts.length))
  let prefix = 0
  counts.forEach((c, e) => {
    entries[4 * e] = prefix
    prefix += c
  })
  return { entries, drawn: prefix }
}

/** the shader's 12-step binary search over the whole table */
function fullSearch(entries: Uint32Array, k: number, v: number): number {
  let lo = 0
  let hi = k
  for (let i = 0; i < 12; i++) {
    if (hi - lo <= 1) break
    const mid = (lo + hi) >> 1
    if (entries[4 * mid] <= v) lo = mid
    else hi = mid
  }
  return lo
}

/** the shader's bounded search (fetchNode.ts with the block index): [entry, steps] */
function blockSearch(entries: Uint32Array, block: Uint32Array, v: number): [number, number] {
  const b = v >> PC.drawIndexBlockLog2
  let lo = block[b]
  let hi = block[b + 1] + 1
  let steps = 0
  for (let i = 0; i < PC.drawIndexBlockLog2 + 1; i++) {
    if (hi - lo <= 1) break
    steps++
    const mid = (lo + hi) >> 1
    if (entries[4 * mid] <= v) lo = mid
    else hi = mid
  }
  return [lo, steps]
}

function check(counts: readonly number[]): number {
  const { entries, drawn } = table(counts)
  const k = counts.length
  const block = new Uint32Array(Math.ceil(drawn / DRAW_BLOCK) + 2)
  const n = buildBlockIndex(entries, k, drawn, block)
  expect(n).toBe(Math.ceil(drawn / DRAW_BLOCK) + 1)
  let maxSteps = 0
  let bad = -1
  for (let v = 0; v < drawn && bad < 0; v++) {
    const [e, steps] = blockSearch(entries, block, v)
    // the same entry as the full search, and it really contains v
    if (e !== fullSearch(entries, k, v) || entries[4 * e] > v || (e + 1 < k && entries[4 * (e + 1)] <= v)) bad = v
    maxSteps = Math.max(maxSteps, steps)
  }
  expect(bad, 'first vertex whose entry differs').toBe(-1)
  return maxSteps
}

describe('DrawTable block index (FX2-R2)', () => {
  it('matches the full binary search for every vertex', () => {
    expect(DRAW_BLOCK).toBe(64)
    check([1])
    check([64])
    check([65])
    check([1000, 2000, 3])
    check([0, 5, 0, 0, 70, 1, 1, 1, 200, 0]) // zero-length entries resolve like the binary search
    // pseudo-random tables: many short entries (prefix nodes) mixed with large nodes
    let seed = 7
    const rnd = (): number => ((seed = (seed * 1103515245 + 12345) >>> 0) / 4294967296)
    for (let t = 0; t < 12; t++) {
      const k = 1 + Math.floor(rnd() * 200)
      const counts = Array.from({ length: k }, () => (rnd() < 0.3 ? Math.floor(rnd() * 8) : Math.floor(rnd() * 1500)))
      if (counts.reduce((a, b) => a + b, 0) === 0) counts[0] = 1
      check(counts)
    }
  })

  it('entries larger than a block need no search step; the bound holds for a block of 1-point entries', () => {
    expect(check(Array.from({ length: 40 }, () => 512))).toBe(1)
    expect(check(Array.from({ length: 300 }, () => 1))).toBeLessThanOrEqual(PC.drawIndexBlockLog2 + 1)
  })
})
