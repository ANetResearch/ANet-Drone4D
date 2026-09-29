// Drone picking by CPU ray-sphere (M06 §6.12, FR-061, AC-043; AWR-14 §6.6). Owner: M06.
// The ray is in world ENU (origin o, unit direction d, from the camera whose projection includes the view offset).
// Sphere radius r = max(R_vis, 6 px of world size at the sphere distance) (hot zone >= 12 CSS px); the nearest entry
// point wins. About 1000 scalar iterations for 1000 vehicles (< 0.2 ms). Zero allocation: the result object is reused.
import type { DronePoseSoA } from '../time/interpRing'

export const DRONE_PICK = { hotZoneCssPx: 6, rVisM: 0.6 } as const

export interface DronePickHit { index: number; t: number }

/**
 * @param wppK world size per CSS px per metre of distance: 2 tan(fovY / 2) / cssH
 * @param skip optional per-index skip (hidden vehicles), 1 = skip
 */
export function pickDroneRay(o: ArrayLike<number>, d: ArrayLike<number>, poses: DronePoseSoA, wppK: number, near: number, out: DronePickHit,
  rVis: number = DRONE_PICK.rVisM, skip: Uint8Array | null = null): DronePickHit {
  let best = Number.POSITIVE_INFINITY
  let hit = -1
  const ox = o[0], oy = o[1], oz = o[2]
  const dx = d[0], dy = d[1], dz = d[2]
  const hz = DRONE_PICK.hotZoneCssPx * wppK
  for (let i = 0; i < poses.n; i++) {
    if (skip !== null && skip[i] === 1) continue
    const cx = poses.pos[3 * i] - ox
    const cy = poses.pos[3 * i + 1] - oy
    const cz = poses.pos[3 * i + 2] - oz
    const tc = cx * dx + cy * dy + cz * dz
    if (tc < near) continue
    const r = Math.max(rVis, hz * tc)
    const d2 = cx * cx + cy * cy + cz * cz - tc * tc
    const r2 = r * r
    if (d2 > r2) continue
    const t = tc - Math.sqrt(r2 - d2)
    if (t < best) {
      best = t
      hit = i
    }
  }
  out.index = hit
  out.t = best
  return out
}

/** brute-force reference (tests): the same criterion with no early outs */
export function pickDroneBrute(o: ArrayLike<number>, d: ArrayLike<number>, pos: ArrayLike<number>, n: number, wppK: number, near: number, rVis: number = DRONE_PICK.rVisM): number {
  let best = Number.POSITIVE_INFINITY
  let hit = -1
  for (let i = 0; i < n; i++) {
    const c = [pos[3 * i] - o[0], pos[3 * i + 1] - o[1], pos[3 * i + 2] - o[2]]
    const tc = c[0] * d[0] + c[1] * d[1] + c[2] * d[2]
    if (tc < near) continue
    const r = Math.max(rVis, DRONE_PICK.hotZoneCssPx * wppK * tc)
    const dist2 = c[0] ** 2 + c[1] ** 2 + c[2] ** 2 - tc * tc
    if (dist2 > r * r) continue
    const t = tc - Math.sqrt(r * r - dist2)
    if (t < best) {
      best = t
      hit = i
    }
  }
  return hit
}
