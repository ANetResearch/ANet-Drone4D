// Deterministic jitter for chart marks (rung length, tick height, mark opacity; AWR-15 §9): the same pair (i, k) always
// gives the same value in [0, 1), so every render and every reload draws the same marks (lint LF-CHART-01 keeps
// Math.random out of chart code). Independent implementation (P4-UI, ADR-072): the pair is folded into one 32-bit word by
// a Weyl step of the golden-ratio constant, then mixed by the 32-bit MurmurHash3 finaliser (fmix32; Austin Appleby,
// public domain) and scaled by 2^-32. No third-party code.
const GOLDEN = 0x9e3779b9
const SCALE = 1 / 4294967296

export function rnd(i: number, k: number): number {
  let h = (Math.imul(i | 0, GOLDEN) + Math.imul(k | 0, 0x632be5ab) + 0x7f4a7c15) | 0
  h = Math.imul(h ^ (h >>> 16), 0x85ebca6b)
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35)
  h ^= h >>> 16
  return (h >>> 0) * SCALE
}
