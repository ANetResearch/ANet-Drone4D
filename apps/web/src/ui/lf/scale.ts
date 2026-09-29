// Scales and unit choice for lieflat charts (d01 §3.8; AWR-15 §9.3): "one rung = one honest unit" needs a nice unit.
/** a nice step >= raw from {1, 2, 2.5, 5} x 10^k */
export function niceStep(raw: number): number {
  if (!(raw > 0) || !Number.isFinite(raw)) return 1
  const p = 10 ** Math.floor(Math.log10(raw))
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p
  return 10 * p
}
export const linear = (d0: number, d1: number, r0: number, r1: number) => {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0)
  return (v: number): number => r0 + (v - d0) * k
}
