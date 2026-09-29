// M06-AC-025 (AWR-03 §3.8, §8.5; M06 §6.9): screen radius buckets in CSS px (identical for Tier S DPR 0.5 and Tier B
// DPR 2), caps (Tier S low-poly <= 32, hero <= 2; B 300 / 6), priority red > selected > critical > r_px for the capped
// slots, 15 % hysteresis (<= 2 bucket switches in 10 s near a threshold), 10 Hz re-bucketing, hidden FPV vehicle.
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera } from 'three'
import { BUCKET, Buckets, newDronePoseSoA, type BucketInputs } from '@/engine'

function cam(): PerspectiveCamera {
  const c = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
  c.position.set(0, 0, 0) // three frame at the ENU origin, looking along three -Z = +N
  c.lookAt(0, 0, -1)
  c.updateMatrixWorld()
  return c
}
function inputs(mark = new Uint8Array(65536), alert = new Uint8Array(65536), hero = true): BucketInputs {
  return { mark, alert, rVisOf: () => 0.6, heroOf: () => hero }
}
/** vehicles along +N at distances d (ENU: x = small offset, y = d) */
function poses(ds: number[]) {
  const p = newDronePoseSoA(1024)
  p.n = ds.length
  ds.forEach((d, i) => {
    p.agentNo[i] = i
    p.pos[3 * i] = (i % 10) * 0.01
    p.pos[3 * i + 1] = d
    p.pos[3 * i + 2] = 0
  })
  return p
}
const K = (cssH: number): number => cssH / (2 * Math.tan(Math.PI / 6))

describe('drone buckets (M06-AC-025)', () => {
  it('thresholds 4 px and 48 px in CSS px, the same on every DPR', () => {
    const k = K(720)
    const dLow = (0.6 * k) / 10 // r_px = 10
    const dHero = (0.6 * k) / 60 // r_px = 60
    const dMarker = (0.6 * k) / 2 // r_px = 2
    const p = poses([dMarker, dLow, dHero])
    const b = new Buckets(1024, 'S')
    const inp = inputs()
    b.radii(p, cam(), 720, inp)
    b.assign(p, inp, 0)
    expect([b.bucket[0], b.bucket[1], b.bucket[2]]).toEqual([BUCKET.MARKER, BUCKET.LOW, BUCKET.HERO])
    expect(b.rpx[1]).toBeCloseTo(10, 5)
    // Tier B at DPR 2: r_px is computed from CSS height only (drawing buffer size does not enter)
    const bB = new Buckets(1024, 'B')
    bB.radii(p, cam(), 720, inp)
    expect(Array.from(bB.rpx.slice(0, 3))).toEqual(Array.from(b.rpx.slice(0, 3)))
  })

  it('caps: 200 near vehicles on Tier S -> 2 hero, 32 low-poly, the rest markers; red and selected win the slots', () => {
    const k = K(720)
    const ds = Array.from({ length: 200 }, (_, i) => (0.6 * k) / (60 + i)) // all above 48 px
    const p = poses(ds)
    const mark = new Uint8Array(65536)
    mark[150] = 2 // red entity (farthest ones have the largest r_px here, so 150 is not in the natural top)
    mark[120] = 1 // selected
    const b = new Buckets(1024, 'S')
    const inp = inputs(mark)
    b.radii(p, cam(), 720, inp)
    b.assign(p, inp, 0)
    let hero = 0, low = 0, marker = 0
    for (let i = 0; i < 200; i++) {
      if (b.bucket[i] === BUCKET.HERO) hero++
      else if (b.bucket[i] === BUCKET.LOW) low++
      else marker++
    }
    expect([hero, low, marker]).toEqual([2, 32, 166])
    expect(b.bucket[150]).toBe(BUCKET.HERO)
    expect(b.bucket[120]).toBe(BUCKET.HERO)
    // governor step 4: 32 -> 16
    b.lowCap = 16
    b.assign(p, inp, 100)
    let low2 = 0
    for (let i = 0; i < 200; i++) if (b.bucket[i] === BUCKET.LOW) low2++
    expect(low2).toBe(16)
    // no hero model: hero candidates fall to low-poly
    const b3 = new Buckets(1024, 'B')
    const inp3 = inputs(mark, new Uint8Array(65536), false)
    b3.radii(p, cam(), 720, inp3)
    b3.assign(p, inp3, 0)
    let h3 = 0, l3 = 0
    for (let i = 0; i < 200; i++) {
      if (b3.bucket[i] === BUCKET.HERO) h3++
      if (b3.bucket[i] === BUCKET.LOW) l3++
    }
    expect([h3, l3]).toEqual([0, 200])
  })

  it('15 % hysteresis: oscillating around 4 px switches at most twice in 10 s', () => {
    const k = K(720)
    const b = new Buckets(1024, 'S')
    const inp = inputs()
    let switches = 0
    let prev: number = -1
    for (let t = 0; t < 10_000; t += 100) {
      // 1 s at 5 px (enters low-poly), then +-12 % around the 4 px threshold (inside the 15 % band)
      const r = t < 1000 ? 5 : 4 * (1 + 0.12 * Math.sin((2 * Math.PI * t) / 2000))
      const p = poses([(0.6 * k) / r])
      b.radii(p, cam(), 720, inp)
      b.assign(p, inp, t)
      if (prev !== -1 && b.bucket[0] !== prev) switches++
      prev = b.bucket[0]
    }
    expect(switches).toBeLessThanOrEqual(2)
    expect(b.bucket[0]).toBe(BUCKET.LOW)
  })

  it('vehicles outside the frustum do not take low-poly slots; the FPV focus vehicle is hidden', () => {
    const k = K(720)
    const p = poses([(0.6 * k) / 20, -(0.6 * k) / 20])
    const mark = new Uint8Array(65536)
    const b = new Buckets(1024, 'S')
    const inp = inputs(mark)
    b.radii(p, cam(), 720, inp)
    b.assign(p, inp, 0)
    expect(b.inFrustum[0]).toBe(1)
    expect(b.inFrustum[1]).toBe(0)
    expect(b.bucket[1]).toBe(BUCKET.MARKER)
    mark[0] = 4
    b.assign(p, inp, 100)
    expect(b.bucket[0]).toBe(BUCKET.HIDDEN)
  })
})
