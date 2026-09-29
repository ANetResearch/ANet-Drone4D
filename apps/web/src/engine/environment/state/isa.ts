// ISA thermodynamics (M07-FR-015; M07 §6.3.11). Owner: M07. Mirror of python/awr/environment/atmosphere/isa.py.
// out = [temperature_c, pressure_pa, rho_kgm3]
export function isa(hMslM: number, isaDtC: number, out: Float64Array): Float64Array {
  const tIsa = 288.15 - 0.0065 * hMslM
  const t = tIsa + isaDtC
  const p = 101325.0 * (tIsa / 288.15) ** 5.25588
  out[0] = t - 273.15
  out[1] = p
  out[2] = p / (287.053 * t)
  return out
}

/** wind library sector slots (M07-FR-052, D1 stub; golden sector_slots.json): [fileIndex, weight, sign, aDeg] x 2 */
export function sectorSlots(thetaFrom: number, sectorsDeg: readonly number[], antisymmetric: boolean): [number, number, number, number][] {
  const full: [number, number, number][] = []
  sectorsDeg.forEach((a, i) => {
    full.push([i, a, 1.0])
    if (antisymmetric) full.push([i, pymod(a + 180.0, 360.0), -1.0])
  })
  full.sort((x, y) => x[1] - y[1])
  const dd = pymod(thetaFrom, 360.0)
  let j = full.findIndex((x) => x[1] > dd)
  if (j < 0) j = 0
  const lo = full[(j - 1 + full.length) % full.length]
  const hi = full[j]
  const span = pymod(hi[1] - lo[1], 360.0) || 360.0
  const w = pymod(dd - lo[1], 360.0) / span
  return [[lo[0], 1.0 - w, lo[2], lo[1]], [hi[0], w, hi[2], hi[1]]]
}

function pymod(a: number, b: number): number {
  let r = a % b
  if (r !== 0) {
    if (b < 0 !== r < 0) r += b
  } else r = b < 0 ? -0 : 0
  return r
}
