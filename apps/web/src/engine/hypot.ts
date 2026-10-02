// Allocation-free Math.hypot for the per-frame paths (AWR-03 §3.6 rule 1; D1-AC-30; FX2-R2). Owner: M06.
// V8 implements Math.hypot as a varargs builtin that copies its arguments into a new FixedDoubleArray on every call
// (src/builtins/math.tq MathHypot): in flight60 scene=full it was the largest allocation site of our code (2.1 MB in
// 20 s, from the point-cloud selection, interpolation and camera code). These functions repeat V8's algorithm step by
// step (maximum absolute value, Kahan-compensated sum of the squared ratios, sqrt times the maximum; Infinity before
// NaN, 0 for all zeros), so their results are bit-identical to Math.hypot: the g02 selector oracle, which calls
// Math.hypot, still matches the product exactly (tests/perf/hypot.test.ts checks the identity).

/** Math.hypot(a, b), bit-identical, no allocation */
export function hypot2(a: number, b: number): number {
  const x = Math.abs(a)
  const y = Math.abs(b)
  if (x === Infinity || y === Infinity) return Infinity
  if (x !== x || y !== y) return Number.NaN
  const max = x > y ? x : y
  if (max === 0) return 0
  let sum = 0
  let comp = 0
  let n = x / max
  let s = n * n - comp
  let p = sum + s
  comp = p - sum - s
  sum = p
  n = y / max
  s = n * n - comp
  p = sum + s
  sum = p
  return Math.sqrt(sum) * max
}

/** Math.hypot(a, b, c), bit-identical, no allocation */
export function hypot3(a: number, b: number, c: number): number {
  const x = Math.abs(a)
  const y = Math.abs(b)
  const z = Math.abs(c)
  if (x === Infinity || y === Infinity || z === Infinity) return Infinity
  if (x !== x || y !== y || z !== z) return Number.NaN
  let max = x > y ? x : y
  if (z > max) max = z
  if (max === 0) return 0
  let sum = 0
  let comp = 0
  let n = x / max
  let s = n * n - comp
  let p = sum + s
  comp = p - sum - s
  sum = p
  n = y / max
  s = n * n - comp
  p = sum + s
  comp = p - sum - s
  sum = p
  n = z / max
  s = n * n - comp
  p = sum + s
  sum = p
  return Math.sqrt(sum) * max
}

/** Math.hypot(a, b, c, d), bit-identical, no allocation */
export function hypot4(a: number, b: number, c: number, d: number): number {
  const x = Math.abs(a)
  const y = Math.abs(b)
  const z = Math.abs(c)
  const w = Math.abs(d)
  if (x === Infinity || y === Infinity || z === Infinity || w === Infinity) return Infinity
  if (x !== x || y !== y || z !== z || w !== w) return Number.NaN
  let max = x > y ? x : y
  if (z > max) max = z
  if (w > max) max = w
  if (max === 0) return 0
  let sum = 0
  let comp = 0
  let n = x / max
  let s = n * n - comp
  let p = sum + s
  comp = p - sum - s
  sum = p
  n = y / max
  s = n * n - comp
  p = sum + s
  comp = p - sum - s
  sum = p
  n = z / max
  s = n * n - comp
  p = sum + s
  comp = p - sum - s
  sum = p
  n = w / max
  s = n * n - comp
  p = sum + s
  sum = p
  return Math.sqrt(sum) * max
}
