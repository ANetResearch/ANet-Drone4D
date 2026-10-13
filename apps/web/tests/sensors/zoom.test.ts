// M13-AC-208 (D2, TS side): withFocal against the Python golden (tests/sensors/golden/zoom.json: GX40 at five focal lengths and
// two outputs, pixels <= 1e-6 px), the critically damped focal follow (tau = 0.15 s), payload ingestion into the sensor cache
// (fx, fy, hfov follow the server focal length), the generated model table against the YAML values and the store mapping.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { fovAtFocal, pixelOfSensorPoint, withFocal, zoomGeomOf, type SensorGeom } from '../../src/engine/sensors/intrinsics'
import { SensorCache } from '../../src/engine/sensors/sensorCache'
import { SENSOR_MODELS, type SensorSpecJson } from '../../src/engine/sensors/specs.gen'
import { FOCAL_TAU_S, dampFocal, focalMm, focalState, setFocalTarget } from '../../src/engine/sensors/zoomTrack'
import { onPayloadData } from '../../src/engine/sensors'
import { payloadFromMsg } from '../../src/stores/sensors'

interface ZoomCase {
  output: string
  f_mm: number
  crop_mm: [number, number]
  w: number
  h: number
  fx: number
  fy: number
  cx: number
  cy: number
  hfov_rad: number
  vfov_rad: number
  uv: number[]
}
const gold = JSON.parse(readFileSync(new URL('../../../../tests/sensors/golden/zoom.json', import.meta.url), 'utf8')) as {
  f_range_mm: [number, number]
  pts_flu: number[]
  cases: ZoomCase[]
}

const gx40 = SENSOR_MODELS.eo_gx40.zoom!

function geomFor(output: string): SensorGeom {
  const z = zoomGeomOf(gx40, output)
  return {
    w: z.w, h: z.h, fx: 0, fy: 0, cx: z.w / 2, cy: z.h / 2, mount: new Float64Array(16), hasGimbal: true, gimbal: { az: 0, el: 0 },
    zoom: { cropWmm: z.cropWmm, cropHmm: z.cropHmm, fMinMm: z.fMinMm, fMaxMm: z.fMaxMm },
  }
}

describe('withFocal (M13-AC-208)', () => {
  it('matches the Python intrinsics, FOV and pixels at five focal lengths', () => {
    expect(gold.cases.length).toBe(10)
    const out = geomFor('1080p')
    const uv = new Float64Array(2)
    const fov = new Float64Array(2)
    for (const c of gold.cases) {
      const s = geomFor(c.output)
      expect(s.zoom!.cropWmm).toBeCloseTo(c.crop_mm[0], 12)
      withFocal(s, c.f_mm, out)
      expect(out.w).toBe(c.w)
      expect(out.h).toBe(c.h)
      expect(Math.abs(out.fx - c.fx)).toBeLessThan(1e-9 * c.fx)
      expect(Math.abs(out.fy - c.fy)).toBeLessThan(1e-9 * c.fy)
      expect(out.cx).toBe(c.cx)
      fovAtFocal(s.zoom!, c.f_mm, fov)
      expect(fov[0]).toBeCloseTo(c.hfov_rad, 12)
      expect(fov[1]).toBeCloseTo(c.vfov_rad, 12)
      for (let i = 0; i < gold.pts_flu.length / 3; i++) {
        pixelOfSensorPoint(out, gold.pts_flu[3 * i], gold.pts_flu[3 * i + 1], gold.pts_flu[3 * i + 2], uv)
        expect(Math.abs(uv[0] - c.uv[2 * i])).toBeLessThanOrEqual(1e-6)
        expect(Math.abs(uv[1] - c.uv[2 * i + 1])).toBeLessThanOrEqual(1e-6)
      }
    }
  })

  it('copies fixed-lens sensors unchanged and writes in place', () => {
    const s: SensorGeom = { w: 640, h: 512, fx: 686.3, fy: 686.3, cx: 320, cy: 256, mount: new Float64Array(16), hasGimbal: false, gimbal: { az: 0, el: 0 } }
    const out = withFocal(s, 30, { ...s, fx: 0, fy: 0 })
    expect(out.fx).toBe(686.3)
    const z = geomFor('1080p')
    withFocal(z, 48, z)
    expect(z.fx).toBeCloseTo((48 / 2.9e-3), 6)
  })
})

describe('focal follow (tau = 0.15 s)', () => {
  it('converges critically damped in ln f without overshoot', () => {
    const st = focalState(4.8)
    setFocalTarget(st, 48)
    const dt = 1 / 60
    let t = 0
    let prev = focalMm(st)
    while (t < 6 * FOCAL_TAU_S) {
      dampFocal(st, dt, false)
      const f = focalMm(st)
      expect(f).toBeGreaterThanOrEqual(prev - 1e-12)
      expect(f).toBeLessThanOrEqual(48 + 1e-9)
      prev = f
      t += dt
    }
    expect(Math.abs(st.lnF - Math.log(48)) / Math.log(10)).toBeLessThan(0.02)
    // at t = tau the remaining error of a step is (1 + 1)·e^-1 = 0.736 of the initial one
    const s2 = focalState(10)
    setFocalTarget(s2, 20)
    dampFocal(s2, FOCAL_TAU_S, false)
    expect((Math.log(20) - s2.lnF) / Math.log(2)).toBeCloseTo(2 * Math.exp(-1), 9)
    dampFocal(s2, 0.01, true)
    expect(focalMm(s2)).toBeCloseTo(20, 12)
  })
})

describe('payload ingestion (M13-FR-209, FR-217)', () => {
  const eoSpec: SensorSpecJson = {
    name: 'eo', kind: 'camera', kindCode: 0, sensorNo: 5, w: 1920, h: 1080, fx: 4.8 / 2.9e-3, fy: 4.8 / 2.9e-3, cx: 960, cy: 540,
    hfov: 1.0511684, vfov: 0.630724, rangeM: 1000, rateHz: 30, mount: [1, 0, 0, 0.12, 0, 1, 0, 0, 0, 0, 1, -0.08, 0, 0, 0, 1], conf: 'B',
    gimbal: { default_mode: 'fixed', default_pitch_deg: -15, default_yaw_deg: 0, pitch_max_deg: 30, pitch_min_deg: -90, rate_max_deg_s: 90, yaw_max_deg: 160, yaw_min_deg: -160 },
    capability: null, modelId: 'eo_gx40', zoom: gx40,
  }

  it('drives fx and hfov of the zoom view towards the payload focal length', () => {
    const c = new SensorCache()
    c.setModelSpecs('p600v2', [eoSpec])
    c.modelOf = () => 'p600v2'
    const v = c.sensorsOf(7)[0]
    expect(v.zoom).not.toBeNull()
    expect(v.fMm).toBeCloseTo(4.8, 12)
    expect(v.hfov * 180 / Math.PI).toBeCloseTo(60.23, 2)
    const ok = onPayloadData({ topic: 'uav/p1/payload', data: { uav: 'p1', sensors: [{ sensor: 'eo', f_mm: 48 }] } }, () => -1)
    expect(ok).toBe(false) // unknown vehicle
    c.onPayload(7, { uav: 'p1', sensors: [{ sensor: 'eo', f_mm: 48, hfov_rad: 0.11587 }] })
    for (let i = 0; i < 120; i++) c.update(1000 / 60, Number.POSITIVE_INFINITY, false)
    expect(v.fMm).toBeCloseTo(48, 2)
    expect(v.fx).toBeCloseTo((v.fMm * 1920) / 5.568, 6)
    expect(v.hfov * 180 / Math.PI).toBeCloseTo(6.64, 1)
    expect(c.payloadOf(7)?.sensors?.[0].f_mm).toBe(48)
    c.onPayload(7, { sensors: [{ sensor: 'eo', f_mm: 4.8 }] })
    c.update(1, Number.POSITIVE_INFINITY, true) // reduced motion jumps
    expect(v.fMm).toBeCloseTo(4.8, 9)
  })

  it('maps a payload sample for the store', () => {
    const p = payloadFromMsg({
      uav: 'p1', gimbal: { pitch_rad: -Math.PI / 4, yaw_rad: 0, mode: 'cue', track_target: null, src: 'cue' },
      sensors: [{ sensor: 'eo', kind: 'eo', active: true, f_mm: 24, hfov_rad: 0.2309, vfov_rad: 0.1304, mode: 'nir', resolution: '1080p',
        palette: null, cue: 'on', illuminator: { on: true, kind: 'builtin', power_w: 0.8, divergence_rad: 0.2309, range_m: 99.7 } },
      { sensor: 'mid360', kind: 'lidar', active: true, f_mm: null, mode: 'paused' }],
    })!
    expect(p.gimbalSrc).toBe('cue')
    expect(p.pitchDeg).toBeCloseTo(-45, 9)
    expect(p.sensors[0]).toMatchObject({ sensor: 'eo', fMm: 24, mode: 'nir', illumOn: true, illumRangeM: 99.7, cue: 'on' })
    expect(p.sensors[0].hfovDeg).toBeCloseTo(13.23, 2)
    expect(p.sensors[1]).toMatchObject({ mode: 'paused', fMm: null, illumOn: null })
    expect(payloadFromMsg(undefined)).toBeNull()
  })
})

describe('generated model table', () => {
  it('carries the GX40 and EO-Z8 zoom and the illuminators of the YAML model files', () => {
    expect(gx40.fMinMm).toBe(4.8)
    expect(gx40.fMaxMm).toBe(48)
    expect(gx40.fNumMin).toBe(1.7)
    expect(gx40.fNumMax).toBe(3.2)
    expect(gx40.zoomTimeS).toBe(4)
    expect(gx40.analysisDefault).toBe('1080p')
    expect(gx40.outputs.map((o) => o.name)).toEqual(['4k', '1080p', '720p', 'sxga', '1p3m'])
    expect(gold.f_range_mm).toEqual([gx40.fMinMm, gx40.fMaxMm])
    expect(SENSOR_MODELS.eo_gx40.illum?.followHfov).toBe(true)
    expect(SENSOR_MODELS.nir_ill_l.illum?.powerW).toBe(3)
    expect(SENSOR_MODELS.eo_z8.illum).toBeNull()
    expect(SENSOR_MODELS.lwir640.lenses?.map((l) => l.fMm)).toEqual([13, 25, 75, 100])
  })
})
