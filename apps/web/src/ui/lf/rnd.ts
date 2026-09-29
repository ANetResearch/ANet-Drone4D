// Deterministic jitter for lieflat marks (rung length, tick height and opacity): the gallery's rnd(i, k) hash, so every
// render and every reload draws the same marks (lint LF-CHART-01 forbids Math.random in chart code).
export const rnd = (i: number, k: number): number => Math.abs(((i * 73856093) ^ (k * 19349663)) % 1000) / 1000
