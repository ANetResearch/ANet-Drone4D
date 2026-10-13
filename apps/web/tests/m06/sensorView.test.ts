// D2 WP-11 sensor views in Node: M19's passive k_light curves at their anchors (AWR-04 §6.2: 0.5 lx colour and 0.05 lx
// mono, 0 at E_min, 1 two decades above), the EO look (dark and grey at night, full colour by day), the active NIR profile
// (cone, 1/R^2 from 0.5 R_nir, M19's edge band, nothing beyond R_nir), the LWIR emissivity and path terms, the gain
// range (background centred, span >= 14 K, hot emitter + 3 K), the grey palette (scene token ramp: monotonic, no hue,
// never a red pixel, black-hot the inverse of white-hot), the B-key cycle over the available views, the thermal
// emitter table (projection, nearest first, far-to-near rows, apparent temperatures) and the HUD capture-box projection.
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera } from 'three'
import {
  SV, availableModes, eoLook, followRange, isRedPixel, kLight, lwirRange, nextMode, nirActive, nirPassive, paletteRgb, pathApparentC, surfaceApparentC,
} from '@/viewport/sensorView/math'
import { writeEmitters } from '@/viewport/sensorView/emitters'
import {
  DEFAULT_ENV, EMITTER_STRIDE, availableNow, cycleSensorView, installSensorViewCamera, selectSensor, sensorViewActive, sensorViewInfo, setDigitalZoom,
  setEmitterSource, setSensorView, sv,
} from '@/viewport/sensorView/state'
import { CameraRig, type SensorsApi, type SensorView } from '@/engine'
import { Matrix4 } from 'three'
import { illuminatorsOf } from '@/viewport/sensorView/pass'
import { EMIT_COLS } from '@/viewport/sensorView/materials'
import { projectBox } from '@/viewport/sensorView/hud'
import { vp } from '@/viewport/session'
import { events } from '@/engine'

const enc = (x: number): number => Math.round(255 * (x <= 0.0031308 ? 12.92 * x : 1.055 * Math.pow(x, 1 / 2.4) - 0.055))

function camAt(eye: [number, number, number], target: [number, number, number]): PerspectiveCamera {
  // ENU -> three (E, U, -N)
  const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
  cam.position.set(eye[0], eye[2], -eye[1])
  cam.lookAt(target[0], target[2], -target[1])
  cam.updateMatrixWorld()
  cam.updateProjectionMatrix()
  return cam
}

describe('sensor view light model (AWR-04 §6.2)', () => {
  it('k_light: 0 at E_min, 0.5 one decade above, 1 two decades above; mono above colour', () => {
    expect(kLight(0.5, 0.5)).toBe(0)
    expect(kLight(5, 0.5)).toBeCloseTo(0.5, 12)
    expect(kLight(50, 0.5)).toBe(1)
    expect(nirPassive(0.05)).toBe(0)
    expect(nirPassive(0.5)).toBeCloseTo(0.5, 12)
    for (const e of [0.001, 0.02, 0.3, 3, 40]) expect(kLight(e, SV.eMinMonoLx)).toBeGreaterThanOrEqual(kLight(e, SV.eMinColorLx))
  })
  it('EO look: full colour by day, grey and dark with more noise at night, monotonic in E', () => {
    const day = eoLook(50_000, { gain: 0, sat: 0, noise: 0 })
    expect(day.gain).toBe(1)
    expect(day.sat).toBe(1)
    const night = eoLook(0.001, { gain: 0, sat: 0, noise: 0 })
    expect(night.sat).toBe(0)
    expect(night.gain).toBeLessThan(0.05)
    expect(night.noise).toBeGreaterThan(day.noise)
    let prev = -1
    for (const e of [0.001, 0.01, 0.05, 0.1, 0.5, 1, 5, 50, 500]) {
      const g = eoLook(e, { gain: 0, sat: 0, noise: 0 }).gain
      expect(g).toBeGreaterThanOrEqual(prev)
      prev = g
    }
  })
  it('active NIR: inside the cone up to R_nir, nothing beyond it or outside the cone, 1/R^2 past half the range', () => {
    const th = (6.6 * Math.PI) / 180
    const R = 200
    expect(nirActive(50, 0, th, R)).toBeCloseTo(SV.nirGainMax, 6)
    expect(nirActive(100, 0, th, R)).toBeCloseTo(1, 6)
    expect(nirActive(R, 0, th, R)).toBe(0)
    expect(nirActive(R + 50, 0, th, R)).toBe(0)
    expect(nirActive(100, th, th, R)).toBe(0)
    expect(nirActive(140, 0, th, R)).toBeCloseTo((100 * 100) / (140 * 140), 6)
    // M19 edge band: clamp((R_nir - R) / (0.2 R_nir), 0, 1)
    expect(nirActive(190, 0, th, R)).toBeCloseTo(((100 * 100) / (190 * 190)) * 0.25, 6)
    expect(nirActive(100, 0, 0, R)).toBe(0)
  })
  it('illuminators of a payload: built-in eo (divergence = HFOV), external nir_ill_l, off ones skipped', () => {
    const out = [{ on: false, divergenceRad: 0, rangeM: 0 }, { on: false, divergenceRad: 0, rangeM: 0 }]
    const n = illuminatorsOf({ sensors: [
      { sensor: 'eo', kind: 'eo_zoom', hfov_rad: 0.2, illuminator: { on: true, divergence_rad: 0.115, range_m: 200 } },
      { sensor: 'nir_ill', kind: 'nir_illuminator', illuminator: { on: true, divergence_rad: 0.096, range_m: 464.8 } },
      { sensor: 'x', kind: 'nir_illuminator', illuminator: { on: false, divergence_rad: 0.1, range_m: 100 } },
    ] } as never, out)
    expect(n).toBe(2)
    expect(out[0]).toEqual({ on: true, divergenceRad: 0.115, rangeM: 200 })
    expect(out[1].rangeM).toBeCloseTo(464.8, 6)
    expect(illuminatorsOf(undefined, out)).toBe(0)
  })
})

describe('LWIR temperatures and gain (AWR-04 §6.2 LWIR row)', () => {
  it('emissivity mixes with the sky, the path relaxes to the air temperature', () => {
    expect(surfaceApparentC(30, 1, -20)).toBe(30)
    expect(surfaceApparentC(30, 0.9, -20)).toBeCloseTo(25, 12)
    expect(pathApparentC(30, 10, 0, 1000)).toBe(30)
    expect(pathApparentC(30, 10, 1e-3, 1000)).toBeCloseTo(10 + 20 * Math.exp(-1), 9)
  })
  it('gain range: centred on the background, span >= 14 K, a hot emitter raises the top', () => {
    const r = lwirRange(20, Number.NaN, new Float64Array(2))
    expect(r[0]).toBe(20 - SV.agcBelowK)
    expect(r[1] - r[0]).toBeGreaterThanOrEqual(SV.agcSpanMinK)
    const h = lwirRange(20, 40, new Float64Array(2))
    expect(h[1]).toBe(43)
    const cur = new Float64Array([Number.NaN, Number.NaN])
    followRange(cur, r, 0.016)
    expect(Array.from(cur)).toEqual(Array.from(r))
    followRange(cur, h, SV.agcTauS)
    expect(cur[1]).toBeCloseTo(r[1] + (h[1] - r[1]) * (1 - Math.exp(-1)), 9)
  })
  it('palette: the neutral token ramp, monotonic, no red pixel anywhere, black-hot inverts white-hot', () => {
    const a = new Float64Array(3)
    const b = new Float64Array(3)
    let prev = -1
    for (let i = 0; i <= 256; i++) {
      const t = i / 256
      paletteRgb(t, 'white_hot', a)
      paletteRgb(1 - t, 'black_hot', b)
      expect(Array.from(a)).toEqual(Array.from(b))
      const r = enc(a[0])
      const g = enc(a[1])
      const bl = enc(a[2])
      expect(isRedPixel(r, g, bl)).toBe(false)
      expect(r - Math.max(g, bl)).toBeLessThanOrEqual(0)
      const y = 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2]
      expect(y).toBeGreaterThanOrEqual(prev)
      prev = y
    }
    expect(isRedPixel(220, 20, 20)).toBe(true)
  })
})

describe('view availability and the B key cycle (AWR-14 §20.10.2)', () => {
  it('cycles FPV -> EO -> night -> LWIR -> FPV and skips LWIR without a thermal sensor', () => {
    const all = availableModes(true, true)
    expect(nextMode('fpv', all)).toBe('eo')
    expect(nextMode('eo', all)).toBe('nir')
    expect(nextMode('nir', all)).toBe('lwir')
    expect(nextMode('lwir', all)).toBe('fpv')
    const noTh = availableModes(true, false)
    expect(noTh.lwir).toBe(false)
    expect(nextMode('nir', noTh)).toBe('fpv')
  })
})

describe('thermal emitters and capture boxes', () => {
  it('projects, keeps the nearest, writes rows far to near with apparent temperatures', () => {
    vp.drones = null
    const cam = camAt([0, -60, 20], [0, 0, 1])
    const rows: number[][] = [
      [0, 0, 0.9, 0.5, 0.3, 1.75, 0, 31, Number.NaN, 0.97, 1], // person, absolute 31 deg C
      [10, 20, 0.75, 4.5, 1.8, 1.5, 0.3, Number.NaN, 25, 0.9, 0.3], // car, +25 K, hot core
      [0, -200, 1, 1, 1, 1, 0, 30, Number.NaN, 1, 1], // behind the camera
    ]
    const off = setEmitterSource('t', (out, max) => {
      const n = Math.min(max, rows.length)
      for (let i = 0; i < n; i++) for (let k = 0; k < EMITTER_STRIDE; k++) out[EMITTER_STRIDE * i + k] = rows[i][k]
      return n
    })
    const env = { ...DEFAULT_ENV, tAirC: 12, tSurfaceC: 10, tSkyC: -30, sigmaLwPerM: 2e-4 }
    const data = new Float32Array(4 * EMIT_COLS * SV.maxEmitters)
    const f = writeEmitters(cam, env, data)
    expect(f.count).toBe(2)
    // row 0 is the farther (car), row 1 the nearer (person)
    const rowR = (j: number) => data[j * EMIT_COLS * 4 + 8]
    expect(rowR(0)).toBeGreaterThan(rowR(1))
    const person = 1 * EMIT_COLS * 4
    const r = rowR(1)
    expect(data[person + 5]).toBeCloseTo(pathApparentC(surfaceApparentC(31, 0.97, -30), 12, 2e-4, r), 4)
    expect(Math.abs(data[person])).toBeLessThan(0.05) // on the optical axis
    expect(data[person + 3]).toBeGreaterThan(data[person + 2]) // taller than wide
    const car = 0
    expect(data[car + 7]).toBeCloseTo(Math.sqrt(0.3), 5)
    expect(data[car + 5]).toBeGreaterThan(data[car + 6]) // core hotter than the shell
    expect(f.hotC).toBeCloseTo(Math.max(data[person + 5], data[car + 5]), 4)
    off()
    expect(writeEmitters(cam, env, data).count).toBe(0)
    expect(sv.sourceList.length).toBe(0)
  })
  it('capture box: the projected rectangle contains the projected centre, false behind the camera', () => {
    const cam = camAt([0, -60, 20], [0, 0, 1])
    vp.camera = cam
    vp.cssW = 1280
    vp.cssH = 720
    const out = new Float32Array(4)
    expect(projectBox([0, 0, 0.9], [0.5, 0.3, 1.75], 0, out)).toBe(true)
    expect(out[0]).toBeLessThan(640)
    expect(out[2]).toBeGreaterThan(640)
    expect(out[3] - out[1]).toBeGreaterThan(out[2] - out[0])
    expect(projectBox([0, -200, 1], [1, 1, 1], 0, out)).toBe(false)
    vp.camera = null
  })
})

describe('view controller (AWR-14 §20.10.5)', () => {
  const view = (sensorNo: number, name: string, kind: number, zoom: boolean): SensorView => ({
    agentNo: 3, sensorNo, name, kind, rangeM: 100, hfov: 1, vfov: 0.7, ring: new Float64Array(36), ringHead: 0, valid: true, zoom: zoom ? {} : null,
  })
  const P = new Matrix4().makePerspective(-0.3, 0.3, 0.2, -0.2, 0.2, 20000)
  const api = (views: SensorView[]): SensorsApi => ({
    sensorsOf: () => views, hasCamera: () => views.some((x) => x.kind === 0), frustumCorners: (_s, _l, o) => o,
    T_base_cam: (_s, out) => new Matrix4().toArray(out) as unknown as Float64Array,
    projectionFor: (_s, _a, _n, _f, out) => P.toArray(out) as unknown as Float64Array,
  })
  function rigWith(views: SensorView[]): CameraRig {
    const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
    const r = new CameraRig(cam, null, {
      motionTier: () => 'reduced', sensors: () => vp.sensors,
      onMode: (mode, followLock) => events.emit('camera.mode', { mode, followLock }),
      focusPose: (_a, p, q, v) => {
        p.set([0, 0, 30])
        q.set([0, 0, 0, 1])
        v.set([0, 0, 0])
        return true
      },
    })
    vp.sensors = api(views)
    vp.rig = r
    return r
  }
  it('refuses outside FPV, picks the zoom camera for EO and night and the thermal camera for LWIR, cycles, resets on leaving FPV', () => {
    const off = installSensorViewCamera()
    const r = rigWith([view(0, 'camera', 0, false), view(1, 'thermal', 2, false), view(5, 'eo', 0, true)])
    expect(setSensorView('eo').reason).toBe('not_fpv')
    expect(r.setMode('fpv', 3).ok).toBe(true)
    expect(r.fpvSensor?.name).toBe('camera') // D1 FPV: the first camera
    expect(availableNow()).toEqual({ fpv: true, eo: true, nir: true, lwir: true })
    expect(setSensorView('eo').ok).toBe(true)
    expect(r.fpvSensor?.name).toBe('eo')
    expect(sensorViewActive()).toBe(true)
    expect(cycleSensorView()).toBe('nir')
    expect(r.fpvSensor?.name).toBe('eo')
    expect(cycleSensorView()).toBe('lwir')
    expect(r.fpvSensor?.name).toBe('thermal')
    setDigitalZoom(9)
    expect(sensorViewInfo().digitalZoom).toBe(4)
    expect(cycleSensorView()).toBe('fpv')
    expect(sensorViewInfo().digitalZoom).toBe(1)
    // choosing a sensor: the thermal one selects LWIR, a camera from LWIR goes to EO
    expect(selectSensor(1).ok).toBe(true)
    expect(sv.mode).toBe('lwir')
    expect(selectSensor(0).ok).toBe(true)
    expect([sv.mode, r.fpvSensor?.name, sv.sensorNo]).toEqual(['eo', 'camera', 0])
    // leaving FPV returns to FPV
    r.setMode('orbit', -1)
    expect(sv.mode).toBe('fpv')
    expect(sensorViewActive()).toBe(false)
    off()
    vp.rig = null
    vp.sensors = null
  })
  it('a vehicle without a thermal camera: LWIR unavailable, skipped by the cycle, refused with no_thermal_sensor', () => {
    const off = installSensorViewCamera()
    const r = rigWith([view(0, 'camera', 0, false)])
    expect(r.setMode('fpv', 3).ok).toBe(true)
    expect(availableNow().lwir).toBe(false)
    expect(setSensorView('lwir').reason).toBe('no_thermal_sensor')
    expect(cycleSensorView()).toBe('eo')
    expect(cycleSensorView()).toBe('nir')
    expect(cycleSensorView()).toBe('fpv')
    r.setMode('orbit', -1)
    off()
    vp.rig = null
    vp.sensors = null
  })
})
