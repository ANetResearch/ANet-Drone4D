// L1 gust fronts, client evaluation (M07-FR-011; M07 §6.3.7; g06 §5.4). Owner: M07.
// Mirror of python/awr/environment/wind/gust.py (golden gust.json). A front travels along its frozen direction e_k with
// f_adv S(t); at p: xi = f_adv S - x0 + s0 - e_k . p, G = amp/2 (1 - cos(2 pi xi / lam)) for 0 <= xi <= lam. Expired when
// f_adv S - x0 > s_span + lam. Fronts are created only by the server (RNG stream 6); gustCreate exists for the golden.
import { eDir } from '../state/conventions'
import type { GustEv } from '../state/keyframe'

const TWO_PI = 2.0 * Math.PI
const e2 = new Float64Array(2)

export function gust(px: number, py: number, sT: number, ev: GustEv, fAdv: number): number {
  eDir(ev.dirFromDeg, e2)
  const xi = fAdv * sT - ev.x0 + ev.s0 - (e2[0] * px + e2[1] * py)
  if (xi >= 0.0 && xi <= ev.lam) return 0.5 * ev.amp * (1.0 - Math.cos((TWO_PI * xi) / ev.lam))
  return 0.0
}

export function gustExpired(sT: number, ev: GustEv, fAdv: number): boolean {
  return fAdv * sT - ev.x0 > ev.sSpan + ev.lam
}

/** golden helper (server-side creation, M07 §6.3.7) */
export function gustCreate(id: number, tNs: number, amp: number, dM: number, dirFromDeg: number, sT: number, speedRef: number,
  bmin: readonly number[], bmax: readonly number[], fAdv: number, leadS = 2, bufferM = 200): GustEv {
  eDir(dirFromDeg, e2)
  const ex = e2[0]
  const ey = e2[1]
  const X = fAdv * sT
  let sMin = Number.POSITIVE_INFINITY
  let sMax = Number.NEGATIVE_INFINITY
  for (const x of [bmin[0], bmax[0]]) for (const y of [bmin[1], bmax[1]]) {
    const d = ex * x + ey * y
    sMin = Math.min(sMin, d)
    sMax = Math.max(sMax, d)
  }
  const margin = fAdv * speedRef * leadS + bufferM
  const s0 = sMin - margin
  return { kind: 1, id, tCreateNs: tNs, x0: X, s0, amp, lam: 2.0 * dM, dirFromDeg, sSpan: sMax - s0 }
}
