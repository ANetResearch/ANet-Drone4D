// M13-AC-010, AC-011, AC-003 (TS side): projection vs the OpenCV pixel formula with an offset principal point, the frame
// rect contain rule, T_base_cam, frustum corners and pixels against the Python golden (tests/sensors/golden).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { Matrix4, PerspectiveCamera, Vector3 } from 'three'
import {
  R_FLU_CAM, R_FLU_OPT, T_base_cam, fovOf, frameRect, frustumCorners, gimbalR, mountMatrix, pixelOfSensorPoint, projectionFor,
  type SensorGeom,
} from '@/engine/sensors/intrinsics'

interface GoldCase {
  id: string
  sensor: { w: number; h: number; fx: number; fy: number; cx: number; cy: number }
  mount: number[]
  gimbals: { az_deg: number; el_deg: number; T_base_cam: number[]; corners: Record<string, number[]> }[]
  projection: { aspect: number; near: number; far: number; P: number[]; frameRect: number[] }[]
  pixels: { pts_flu: number[]; uv: number[] }
}
const gold = JSON.parse(readFileSync(new URL('../../../../tests/sensors/golden/intrinsics.json', import.meta.url), 'utf8')) as { cases: GoldCase[] }
const frames = JSON.parse(readFileSync(new URL('../../../../tests/sensors/golden/frames.json', import.meta.url), 'utf8')) as {
  R_FLU_OPT: number[]; R_FLU_CAM: number[]; gimbal: { az_deg: number; el_deg: number; R: number[] }[]; mount: { rpy_deg: number[]; R: number[] }[]
}
const D = Math.PI / 180

function geom(c: GoldCase, azDeg = 0, elDeg = 0): SensorGeom {
  return { ...c.sensor, mount: Float64Array.from(c.mount), hasGimbal: true, gimbal: { az: azDeg * D, el: elDeg * D } }
}
function maxAbs(a: ArrayLike<number>, b: ArrayLike<number>): number {
  let m = 0
  for (let i = 0; i < a.length; i++) m = Math.max(m, Math.abs(a[i] - b[i]))
  return m
}
/** deterministic LCG in [0, 1) (tests may not use Math.random, DET-01) */
function lcg(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0
    return s / 4294967296
  }
}

describe('frame constants (AWR-03 §5.1 rule 7)', () => {
  it('match the Python golden and M02', () => {
    expect(Array.from(R_FLU_CAM)).toEqual([0, 0, -1, -1, 0, 0, 0, 1, 0])
    expect(maxAbs(R_FLU_CAM, frames.R_FLU_CAM)).toBe(0)
    expect(maxAbs(R_FLU_OPT, frames.R_FLU_OPT)).toBe(0)
    const out = new Float64Array(9)
    for (const g of frames.gimbal) expect(maxAbs(gimbalR(g.az_deg * D, g.el_deg * D, out), g.R)).toBeLessThan(1e-12)
    const m16 = new Float64Array(16)
    for (const m of frames.mount) {
      mountMatrix([0, 0, 0], m.rpy_deg, m16)
      const r = [m16[0], m16[1], m16[2], m16[4], m16[5], m16[6], m16[8], m16[9], m16[10]]
      expect(maxAbs(r, m.R)).toBeLessThan(1e-12)
    }
  })
})

describe('projectionFor (M13-AC-010)', () => {
  it('matches the OpenCV pixel formula within 1e-6 px on 1e5 points with an offset principal point', () => {
    const s: SensorGeom = { w: 6000, h: 4000, fx: 5196, fy: 5196, cx: 3037.5, cy: 1978.75, mount: new Float64Array(16), hasGimbal: false, gimbal: { az: 0, el: 0 } }
    const P = projectionFor(s, s.w / s.h, 0.2, 5000, new Float64Array(16))
    const rnd = lcg(7)
    let err = 0
    for (let i = 0; i < 100000; i++) {
      const Z = -(1 + rnd() * 400)
      const X = (rnd() - 0.5) * 2 * Z
      const Y = (rnd() - 0.5) * 2 * Z
      const u = (s.fx * X) / -Z + s.cx
      const v = (s.fy * -Y) / -Z + s.cy
      // column-major P · [X, Y, Z, 1]
      const cx = P[0] * X + P[4] * Y + P[8] * Z + P[12]
      const cy = P[1] * X + P[5] * Y + P[9] * Z + P[13]
      const cw = P[3] * X + P[7] * Y + P[11] * Z + P[15]
      const u2 = ((cx / cw + 1) / 2) * s.w
      const v2 = ((1 - cy / cw) / 2) * s.h
      err = Math.max(err, Math.abs(u - u2), Math.abs(v - v2))
    }
    expect(err).toBeLessThan(1e-6)
  })

  it('is element-wise equal to the golden and loads into a three camera unchanged', () => {
    const out = new Float64Array(16)
    for (const c of gold.cases) {
      for (const p of c.projection) {
        projectionFor(geom(c), p.aspect, p.near, p.far, out)
        expect(maxAbs(out, p.P)).toBeLessThan(1e-12)
      }
    }
    const cam = new PerspectiveCamera()
    cam.projectionMatrix.fromArray(projectionFor(geom(gold.cases[0]), 16 / 9, 0.2, 5000, out))
    expect(maxAbs(cam.projectionMatrix.elements, out)).toBeLessThan(1e-6) // three stores float32-safe numbers
  })
})

describe('frameRect (M13-AC-011)', () => {
  const cam = gold.cases[0]
  it('16:9 viewport on a 3:2 sensor keeps VFOV 42.10 deg', () => {
    const r = frameRect(geom(cam), 16 / 9, new Float64Array(4))
    expect(r[0]).toBeCloseTo(-0.84375, 12)
    expect(r[2]).toBeCloseTo(0.84375, 12)
    expect(r[1]).toBe(-1)
    expect(r[3]).toBe(1)
    const P = projectionFor(geom(cam), 16 / 9, 0.2, 5000, new Float64Array(16))
    expect((2 * Math.atan(1 / P[5])) / D).toBeCloseTo(42.1, 2)
  })
  it('4:3 viewport keeps HFOV 60.00 deg', () => {
    const r = frameRect(geom(cam), 4 / 3, new Float64Array(4))
    expect(r[0]).toBe(-1)
    expect(r[3]).toBeCloseTo((4 / 3) / 1.5, 12)
    const P = projectionFor(geom(cam), 4 / 3, 0.2, 5000, new Float64Array(16))
    expect((2 * Math.atan(1 / P[0])) / D).toBeCloseTo(60.0, 2)
    for (const c of gold.cases) for (const p of c.projection) expect(maxAbs(frameRect(geom(c), p.aspect, new Float64Array(4)), p.frameRect)).toBeLessThan(1e-12)
  })
})

describe('T_base_cam and frustumCorners against the Python golden', () => {
  it('match within 1e-9 (1e-4 m required)', () => {
    const t = new Float64Array(16)
    const f = new Float64Array(15)
    for (const c of gold.cases) {
      for (const g of c.gimbals) {
        const s = geom(c, g.az_deg, g.el_deg)
        expect(maxAbs(T_base_cam(s, t), g.T_base_cam)).toBeLessThan(1e-9)
        for (const [L, exp] of Object.entries(g.corners)) expect(maxAbs(frustumCorners(s, Number(L), f), exp)).toBeLessThan(1e-9)
      }
    }
  })

  it('FPV camera looks along the optical axis and the corners project to the image corners', () => {
    const c = gold.cases[1]
    const s = geom(c, 25, -40)
    const T = new Matrix4().fromArray(T_base_cam(s, new Float64Array(16)))
    const P = new Matrix4().fromArray(projectionFor(s, s.w / s.h, 0.2, 5000, new Float64Array(16)))
    const inv = T.clone().invert()
    const f = frustumCorners(s, 60, new Float64Array(15))
    const want = [[-1, 1], [1, 1], [1, -1], [-1, -1]]
    for (let k = 0; k < 4; k++) {
      const v = new Vector3(f[3 + 3 * k], f[4 + 3 * k], f[5 + 3 * k]).applyMatrix4(inv).applyMatrix4(P)
      expect(v.x).toBeCloseTo(want[k][0], 6)
      expect(v.y).toBeCloseTo(want[k][1], 6)
    }
  })
})

describe('pixels and fov', () => {
  it('pixelOfSensorPoint matches the Python golden within 1e-6 px (M13-AC-008)', () => {
    const out = new Float64Array(2)
    for (const c of gold.cases) {
      const s = geom(c)
      const p = c.pixels.pts_flu
      for (let i = 0; i < p.length / 3; i++) {
        pixelOfSensorPoint(s, p[3 * i], p[3 * i + 1], p[3 * i + 2], out)
        expect(Math.abs(out[0] - c.pixels.uv[2 * i])).toBeLessThan(1e-6)
        expect(Math.abs(out[1] - c.pixels.uv[2 * i + 1])).toBeLessThan(1e-6)
      }
    }
  })
  it('fov of the p600 camera is 60.0 x 42.1 deg', () => {
    const f = fovOf(gold.cases[0].sensor, new Float64Array(2))
    expect(f[0] / D).toBeCloseTo(60.0, 3)
    expect(f[1] / D).toBeCloseTo(42.103, 3)
  })
})
