// Frozen von Karman turbulence box, CPU mirror (M07-FR-012, FR-033; M07 §6.3.8). Owner: M07.
// Same sampling as python/awr/environment/wind/turbulence.py TurbBox.sample (golden turb.json): periodic trilinear with
// the cell-centre convention, g = q / dx - 0.5 (q = p - Dq, Dq = (f_adv D) mod 256 computed in float64 on the CPU). The GPU
// path samples the same f16 asset with texture3D(uvw = q / 256, RepeatWrapping, LinearFilter), which is the same
// interpolation (M07-AC-016). MIL-F-8785C height scaling: sigma_w = 0.5295 sigma_ref, sigma_u = sigma_w / (0.177 +
// 0.000823 h_ft)^0.4, h_ft = max(z_agl / 0.3048, 10).
import type { AwrvVolume } from './awrv'

export const FT = 0.3048
export const SIGMA_W_OVER_REF = (0.177 + 0.000823 * (10.0 / FT)) ** 0.4

/** out = [sigma_u(z), sigma_w] */
export function milSigma(zAgl: number, sigmaRef: number, out: Float64Array): Float64Array {
  const sw = SIGMA_W_OVER_REF * sigmaRef
  const hFt = Math.max(zAgl / FT, 10.0)
  out[0] = sw / (0.177 + 0.000823 * hFt) ** 0.4
  out[1] = sw
  return out
}

export class TurbBoxCPU {
  readonly n: number
  readonly dx: number
  readonly period: number
  private readonly v: Float32Array

  constructor(vol: AwrvVolume) {
    if (!vol.cpu || vol.nx !== vol.ny || vol.ny !== vol.nz || vol.comp !== 4) throw new Error('turbulence box must be a cubic f16/f32 RGBA volume')
    this.n = vol.nx
    this.dx = vol.cell[0]
    this.period = this.n * this.dx
    this.v = vol.cpu
  }

  /** q in metres (already shifted by Dq) -> out[0..2] */
  sample(qx: number, qy: number, qz: number, out: Float64Array): Float64Array {
    const n = this.n
    const gx = qx / this.dx - 0.5
    const gy = qy / this.dx - 0.5
    const gz = qz / this.dx - 0.5
    const fx0 = Math.floor(gx)
    const fy0 = Math.floor(gy)
    const fz0 = Math.floor(gz)
    const wx = gx - fx0
    const wy = gy - fy0
    const wz = gz - fz0
    const i0 = ((fx0 % n) + n) % n
    const j0 = ((fy0 % n) + n) % n
    const k0 = ((fz0 % n) + n) % n
    const i1 = (i0 + 1) % n
    const j1 = (j0 + 1) % n
    const k1 = (k0 + 1) % n
    let a0 = 0
    let a1 = 0
    let a2 = 0
    const v = this.v
    for (let dz = 0; dz < 2; dz++) {
      const kz = dz ? k1 : k0
      const wz_ = dz ? wz : 1 - wz
      for (let dy = 0; dy < 2; dy++) {
        const jy = dy ? j1 : j0
        const wy_ = dy ? wy : 1 - wy
        const row = (kz * n + jy) * n
        for (let dxx = 0; dxx < 2; dxx++) {
          const ix = dxx ? i1 : i0
          const w = wz_ * wy_ * (dxx ? wx : 1 - wx)
          const o = 4 * (row + ix)
          a0 += v[o] * w
          a1 += v[o + 1] * w
          a2 += v[o + 2] * w
        }
      }
    }
    out[0] = a0
    out[1] = a1
    out[2] = a2
    return out
  }
}
