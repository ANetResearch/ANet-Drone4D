// Deterministic spherical scatter of the field view (AWR-04 §15.11.2; ADR-125; M19 python/awr/sim/perception/field.py
// pattern()). Owner: M06.
// Point i (0-based) of anchor j: h1 = H2(i+1), h2 = H3(i+1), h3 = H5(i+1) (Halton); direction z = 1 - 2 h2,
// phi = 2 pi h3 + j * 2.39996 (golden angle: neighbouring anchors do not repeat the same picture); radius
// r = r_min (r_max / r_min)^h1 (log-uniform: dense near the anchor, sparse far away, matching a field that falls off with
// distance). The pattern is fixed for a session, so value refreshes never move a point; the server's explicit samples
// (SB-78) use the same rule, so the two can be compared point by point.
export const PATTERN = { goldenAngle: 2.39996, rMinUav: 2, rMinBody: 1, maxPerAnchor: 1024 } as const

/** Halton radical inverse H_base(i), i >= 1 */
export function halton(i: number, base: number): number {
  let f = 1
  let r = 0
  let k = i
  while (k > 0) {
    f /= base
    r += f * (k % base)
    k = Math.floor(k / base)
  }
  return r
}

// the three sequences for i = 1..maxPerAnchor, computed once
const H1 = new Float64Array(PATTERN.maxPerAnchor)
const H2 = new Float64Array(PATTERN.maxPerAnchor)
const H3 = new Float64Array(PATTERN.maxPerAnchor)
for (let i = 0; i < PATTERN.maxPerAnchor; i++) {
  H1[i] = halton(i + 1, 2)
  H2[i] = halton(i + 1, 3)
  H3[i] = halton(i + 1, 5)
}

/** h1 of point i (the log-radius coordinate) */
export const patternH1 = (i: number): number => H1[i]

/**
 * unit direction of point i of anchor j into out[o..o+2] (ENU); returns h1. i < PATTERN.maxPerAnchor
 */
export function patternDir(i: number, j: number, out: { [k: number]: number }, o = 0): number {
  const z = 1 - 2 * H2[i]
  const phi = 2 * Math.PI * H3[i] + j * PATTERN.goldenAngle
  const s = Math.sqrt(Math.max(0, 1 - z * z))
  out[o] = s * Math.cos(phi)
  out[o + 1] = s * Math.sin(phi)
  out[o + 2] = z
  return H1[i]
}

/** radius of a point with log-radius coordinate h1 */
export const patternRadius = (h1: number, rMin: number, rMax: number): number => rMin * (rMax / rMin) ** h1

/** the first n points of anchor j: directions (3 n) and radii (n), as the server's pattern(n, j, r_min, r_max) */
export function patternInto(n: number, j: number, rMin: number, rMax: number, dirs: { [k: number]: number }, radii: { [k: number]: number }): void {
  const m = Math.min(n, PATTERN.maxPerAnchor)
  for (let i = 0; i < m; i++) radii[i] = patternRadius(patternDir(i, j, dirs, 3 * i), rMin, rMax)
}
