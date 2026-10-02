// Screen-radius buckets (M06-FR-031, FR-032, AC-025; M06 §6.9; AWR-03 §3.8, §8.5; r14 §3.6). Owner: M06.
// Every 100 ms: r_px = R_vis x k / max(d, near) with k = cssH / (2 tan(fovY / 2)) (CSS px, identical on every tier and
// DPR); raw bucket HERO above 48 px, LOW above 4 px, else MARKER, with 15 % hysteresis around the current bucket.
// Only vehicles inside the view frustum compete for the LOW and HERO slots; priority = red entity 8e9 + selected 4e9 +
// alert level 1e9 + r_px; the top K by priority take HERO (cap: S 2, B/A 6), then LOW (S 32 -> 16 -> 8 by PerfGovernor
// step 4; B/A 300 -> 150 -> 75); the rest fall back to MARKER. Models without a hero fall to LOW; the FPV focus vehicle
// is hidden (the camera sits inside it). Fixed-size heaps, zero allocation per call.
import { Frustum, Matrix4, Sphere, Vector3, type PerspectiveCamera } from 'three'
import type { DronePoseSoA } from '../time/interpRing'
import { hypot3 } from '../hypot'

export const BUCKET = { MARKER: 0, LOW: 1, HERO: 2, HIDDEN: 3 } as const
export const BUCKETS = {
  lowPx: 4, heroPx: 48, up: 1.15, down: 0.85, hz: 10,
  heroCapS: 2, heroCapBA: 6, lowCapS: 32, lowCapBA: 300, lowLevelsS: [32, 16, 8], lowLevelsBA: [300, 150, 75],
} as const

export interface BucketInputs {
  /** flags keyed by agentNo: bit0 selected, bit1 red entity, bit2 hidden (FPV focus) */
  readonly mark: Uint8Array
  /** alert level keyed by agentNo: 0 none, 1 warning, 2 critical */
  readonly alert: Uint8Array
  /** R_vis per index (m) and whether the vehicle's model has a hero (per index) */
  rVisOf(i: number): number
  heroOf(i: number): boolean
}

/** min-heap of (priority, index) with a fixed capacity: keeps the K largest priorities */
class TopK {
  readonly idx: Int32Array
  readonly pri: Float64Array
  n = 0
  constructor(readonly cap: number) {
    this.idx = new Int32Array(Math.max(1, cap))
    this.pri = new Float64Array(Math.max(1, cap))
  }
  reset(k: number): void {
    this.n = 0
    this.k = Math.min(k, this.cap)
  }
  k = 0
  push(i: number, p: number): void {
    if (this.k <= 0) return
    if (this.n < this.k) {
      let c = this.n++
      this.idx[c] = i
      this.pri[c] = p
      while (c > 0) {
        const up = (c - 1) >> 1
        if (this.pri[up] <= this.pri[c]) break
        this.swap(c, up)
        c = up
      }
      return
    }
    if (p <= this.pri[0]) return
    this.idx[0] = i
    this.pri[0] = p
    let c = 0
    for (;;) {
      const l = 2 * c + 1
      const r = l + 1
      let m = c
      if (l < this.n && this.pri[l] < this.pri[m]) m = l
      if (r < this.n && this.pri[r] < this.pri[m]) m = r
      if (m === c) break
      this.swap(c, m)
      c = m
    }
  }
  private swap(a: number, b: number): void {
    const ti = this.idx[a]
    this.idx[a] = this.idx[b]
    this.idx[b] = ti
    const tp = this.pri[a]
    this.pri[a] = this.pri[b]
    this.pri[b] = tp
  }
}

export class Buckets {
  /** bucket keyed by agentNo (persistent for hysteresis) */
  readonly byAgent = new Uint8Array(65536)
  /** per SoA index of the last assign: bucket and r_px */
  readonly bucket: Uint8Array
  readonly rpx: Float32Array
  readonly inFrustum: Uint8Array
  private readonly heroTop: TopK
  private readonly lowTop: TopK
  private readonly raw: Uint8Array
  private readonly frustum = new Frustum()
  private readonly m = new Matrix4()
  private readonly sphere = new Sphere()
  private readonly eye = new Vector3()
  heroCap: number
  lowCap: number
  lastMs = Number.NEGATIVE_INFINITY
  heroN = 0
  lowN = 0

  constructor(readonly capacity: number, tier: 'A' | 'B' | 'S') {
    this.bucket = new Uint8Array(capacity)
    this.rpx = new Float32Array(capacity)
    this.inFrustum = new Uint8Array(capacity)
    this.raw = new Uint8Array(capacity)
    this.heroCap = tier === 'S' ? BUCKETS.heroCapS : BUCKETS.heroCapBA
    this.lowCap = tier === 'S' ? BUCKETS.lowCapS : BUCKETS.lowCapBA
    this.heroTop = new TopK(BUCKETS.heroCapBA)
    this.lowTop = new TopK(BUCKETS.lowCapBA)
  }

  /** r_px of every vehicle (CSS px) at the camera; used by the bucket pass and the focus set */
  radii(poses: DronePoseSoA, cam: PerspectiveCamera, cssH: number, inp: BucketInputs): void {
    const k = cssH / (2 * Math.tan((cam.fov * Math.PI) / 360))
    this.eye.setFromMatrixPosition(cam.matrixWorld)
    // camera position three (E, U, -N) -> ENU
    const ex = this.eye.x
    const ey = -this.eye.z
    const ez = this.eye.y
    this.m.multiplyMatrices(cam.projectionMatrix, cam.matrixWorldInverse)
    this.frustum.setFromProjectionMatrix(this.m)
    const near = cam.near
    for (let i = 0; i < poses.n; i++) {
      const x = poses.pos[3 * i]
      const y = poses.pos[3 * i + 1]
      const z = poses.pos[3 * i + 2]
      const d = Math.max(hypot3(x - ex, y - ey, z - ez), near)
      const rv = inp.rVisOf(i)
      this.rpx[i] = (rv * k) / d
      this.sphere.center.set(x, z, -y)
      this.sphere.radius = rv
      this.inFrustum[i] = this.frustum.intersectsSphere(this.sphere) ? 1 : 0
    }
  }

  /** 10 Hz bucket assignment (call radii() first in the same frame) */
  assign(poses: DronePoseSoA, inp: BucketInputs, nowMs: number): void {
    this.lastMs = nowMs
    const n = poses.n
    this.heroTop.reset(this.heroCap)
    this.lowTop.reset(this.lowCap)
    for (let i = 0; i < n; i++) {
      const a = poses.agentNo[i]
      const cur = this.byAgent[a]
      const r = this.rpx[i]
      const heroTh = BUCKETS.heroPx * (cur >= BUCKET.HERO && cur !== BUCKET.HIDDEN ? BUCKETS.down : BUCKETS.up)
      const lowTh = BUCKETS.lowPx * (cur >= BUCKET.LOW && cur !== BUCKET.HIDDEN ? BUCKETS.down : BUCKETS.up)
      let raw: number = r > heroTh ? BUCKET.HERO : r > lowTh ? BUCKET.LOW : BUCKET.MARKER
      if (raw === BUCKET.HERO && !inp.heroOf(i)) raw = BUCKET.LOW
      this.raw[i] = raw
      if ((inp.mark[a] & 4) !== 0 || raw === BUCKET.MARKER || !this.inFrustum[i]) continue
      const pri = ((inp.mark[a] & 2) !== 0 ? 8e9 : 0) + ((inp.mark[a] & 1) !== 0 ? 4e9 : 0) + inp.alert[a] * 1e9 + r
      if (raw === BUCKET.HERO) this.heroTop.push(i, pri)
      else this.lowTop.push(i, pri)
    }
    for (let i = 0; i < n; i++) this.bucket[i] = BUCKET.MARKER
    // hero winners; hero candidates beyond the cap compete for LOW
    for (let j = 0; j < this.heroTop.n; j++) this.bucket[this.heroTop.idx[j]] = BUCKET.HERO
    this.heroN = this.heroTop.n
    for (let i = 0; i < n; i++) {
      if (this.raw[i] !== BUCKET.HERO || this.bucket[i] === BUCKET.HERO || !this.inFrustum[i]) continue
      const a = poses.agentNo[i]
      if ((inp.mark[a] & 4) !== 0) continue
      this.lowTop.push(i, ((inp.mark[a] & 2) !== 0 ? 8e9 : 0) + ((inp.mark[a] & 1) !== 0 ? 4e9 : 0) + inp.alert[a] * 1e9 + this.rpx[i])
    }
    for (let j = 0; j < this.lowTop.n; j++) this.bucket[this.lowTop.idx[j]] = BUCKET.LOW
    this.lowN = this.lowTop.n
    for (let i = 0; i < n; i++) {
      const a = poses.agentNo[i]
      if ((inp.mark[a] & 4) !== 0) this.bucket[i] = BUCKET.HIDDEN
      this.byAgent[a] = this.bucket[i]
    }
  }

  /** between assignments the SoA order may change: refresh per-index buckets from the per-agent table */
  refresh(poses: DronePoseSoA, inp: BucketInputs): void {
    for (let i = 0; i < poses.n; i++) {
      const a = poses.agentNo[i]
      this.bucket[i] = (inp.mark[a] & 4) !== 0 ? BUCKET.HIDDEN : this.byAgent[a]
    }
  }

  forget(agentNo: number): void {
    this.byAgent[agentNo & 0xffff] = BUCKET.MARKER
  }
}
