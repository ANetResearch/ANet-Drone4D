// Focal length visual follow for optical zoom sensors (M13-FR-217; M13 §15.3.2). Owner: M13. The server publishes the
// current focal length of every zoom sensor in uav/{id}/payload at 2 Hz (f_mm, already rate limited to the lens' full
// zoom time); the drawn focal length approaches it with the same critically damped response as the gimbal follow
// (tau = 0.15 s, ADR-046), integrated exactly per frame in ln f (zoom is uniform in ln f), so the FPV field of view and
// the frustum change smoothly between payload samples. The reduced motion tier jumps to the target. No allocation.

export const FOCAL_TAU_S = 0.15

export interface FocalState {
  /** ln of the drawn focal length (mm) */
  lnF: number
  /** ln of the latest server focal length */
  lnFT: number
  /** d lnF / dt (1/s) */
  v: number
}

export function focalState(fMm: number): FocalState {
  const l = Math.log(fMm)
  return { lnF: l, lnFT: l, v: 0 }
}

/** new target focal length (mm) from a payload sample; ignored when not finite or not positive */
export function setFocalTarget(st: FocalState, fMm: number): void {
  if (Number.isFinite(fMm) && fMm > 0) st.lnFT = Math.log(fMm)
}

/** critically damped step towards the target over dtS (exact solution, stable for any dt); reduced jumps */
export function dampFocal(st: FocalState, dtS: number, reduced: boolean, tauS = FOCAL_TAU_S): void {
  if (reduced) {
    st.lnF = st.lnFT
    st.v = 0
    return
  }
  if (!(dtS > 0)) return
  const w = 1 / tauS
  const e = Math.exp(-w * dtS)
  const e0 = st.lnF - st.lnFT
  const c = st.v + w * e0
  st.lnF = st.lnFT + (e0 + c * dtS) * e
  st.v = (st.v - w * c * dtS) * e
}

export function focalMm(st: FocalState): number {
  return Math.exp(st.lnF)
}
