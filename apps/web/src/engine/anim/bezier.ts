// Cubic bezier easing evaluator (AWR-15 §8.7; M06 §6.11 camera flight; M05 §6.7.4 node fade; M12 §6.3 freeze decay).
// Owner: M06. Control points come from lib/tokens/motion.gen.ts EASE (never literals in engine code, ADR-029). Newton
// iterations (8) with a bisection fallback when the derivative is close to 0; no allocation per call.
export type Bezier = readonly [number, number, number, number]

const sample = (a1: number, a2: number, t: number): number => {
  const u = 1 - t
  return 3 * u * u * t * a1 + 3 * u * t * t * a2 + t * t * t
}
const slope = (a1: number, a2: number, t: number): number => {
  const u = 1 - t
  return 3 * u * u * a1 + 6 * u * t * (a2 - a1) + 3 * t * t * (1 - a2)
}

function solve(x1: number, y1: number, x2: number, y2: number, x: number): number {
  if (x <= 0) return 0
  if (x >= 1) return 1
  let t = x
  for (let i = 0; i < 8; i++) {
    const err = sample(x1, x2, t) - x
    if (Math.abs(err) < 1e-7) return sample(y1, y2, t)
    const d = slope(x1, x2, t)
    if (Math.abs(d) < 1e-6) break
    t -= err / d
    if (t < 0 || t > 1) break
  }
  let lo = 0
  let hi = 1
  t = x
  for (let i = 0; i < 30; i++) {
    const v = sample(x1, x2, t)
    if (Math.abs(v - x) < 1e-7) break
    if (v < x) lo = t
    else hi = t
    t = 0.5 * (lo + hi)
  }
  return sample(y1, y2, t)
}

/** y(x) of the curve for x in [0, 1] (clamped) */
export function bezierAt(b: Bezier, x: number): number {
  return solve(b[0], b[1], b[2], b[3], x)
}

/** a reusable easing function for fixed control points (M06 §9.1) */
export function makeBezier(x1: number, y1: number, x2: number, y2: number): (x: number) => number {
  return (x: number) => solve(x1, y1, x2, y2, x)
}
