// M19-AC-017 (Node part), M19-NFR-006: the TS calculator equals the Python reference within 1 % on 200 random inputs of the
// unified criterion, on the geometric feasible-window table and on the reference ranges (fixture calc_cases.json generated
// by tests/perception/test_calc_golden.py); plus the optics anchors of AWR-04 §6.1 and the GX40 FOV within 0.2 deg.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  BAND_LUX, defaultEnv, feasibilityDetail, feasibilityWindow, gsdAtRange, gsdM, gx40, hfovRad, levelOf, lwir640, maxRangeForLevel, N_P90,
  perceptionLevel, pixelsOnTarget, pPrimeUm, sensorFromCatalog, sensorSummary, targetFromClass, ttpf, vfovRad, zoomForLevel, type EnvP, type SensorP,
  type TargetP,
} from '@/engine/perception'

interface Case {
  sensor: { name: string; analysis?: string; night?: boolean; ext?: boolean; lens?: number }
  fMm: number
  mode: 'auto' | 'day' | 'lowlight' | 'nir'
  target: { size: [number, number, number]; contrast0: number; camouflage: number; reflect850: number; thermal: TargetP['thermal']; occlusion: number }
  env: EnvP
  geom: { rangeM: number; epsRad: number; betaRad: number; worst: boolean; kLos: number; tracking: boolean; vRelPerpMps: number; inIllumCone: boolean }
  nReq: number
  expect: { level: number; n: number; nEff: number; p: number; gsdM: number; kC: number; kLight: number; kBlur: number; limiting: string | null; dtAppK: number | null }
}

const FIX = JSON.parse(readFileSync(join(import.meta.dirname, 'calc_cases.json'), 'utf8')) as {
  cases: Case[]
  feasibility: { class: 'person' | 'dog' | 'vehicle'; sensor: string; light: 'day' | 'night'; mor_m: number; d_lo_m: number | null; d_hi_m: number | null; why: string | null }[]
  ref_ranges: { key: string; sensor: Case['sensor']; rows: { target: string; light: string; mor_m: number; D_m: number; R_m: number; I_m: number }[] }[]
}

function sensorOf(s: Case['sensor']): SensorP {
  return s.name === 'gx40' ? gx40(s.analysis ?? '1080p', { night: s.night, ext: s.ext }) : lwir640(s.lens ?? 100)
}

const close = (a: number, b: number, rel = 0.01, abs = 1e-6): boolean => Math.abs(a - b) <= Math.max(abs, rel * Math.max(Math.abs(a), Math.abs(b)))

describe('unified criterion vs Python (M19-AC-017)', () => {
  it('200 random cases within 1 %', () => {
    expect(FIX.cases.length).toBe(200)
    let bad = 0
    for (const c of FIX.cases) {
      const T: TargetP = { ...c.target, criticalDimM: null, levelReq: 'R', nReqPx: null, moving: false, speedMaxMps: 0, bodies: [] }
      const r = perceptionLevel({ sensor: sensorOf(c.sensor), fMm: c.fMm, mode: c.mode, target: T, env: c.env, nReq: c.nReq,
        geom: { ...c.geom, inFov: true } })
      const e = c.expect
      const ok = close(r.n, e.n) && close(r.nEff, e.nEff, 0.01, 1e-4) && close(r.gsdM, e.gsdM) && close(r.k.c, e.kC, 0.01, 1e-4)
        && close(r.k.light, e.kLight, 0.01, 1e-4) && close(r.k.blur, e.kBlur) && close(r.p, e.p, 0.01, 1e-4)
        && (e.dtAppK === null ? r.dtAppK === null : close(r.dtAppK ?? NaN, e.dtAppK, 0.01, 1e-4))
      const nearThreshold = [3.5, 14, 22.4, c.nReq].some((v) => Math.abs(e.nEff - v) <= 0.01 * v)
      const same = r.level === e.level && r.limiting === e.limiting
      if (!ok || (!same && !nearThreshold)) bad++
    }
    expect(bad).toBe(0)
  })
})

describe('feasible window vs the Python golden table (geometric)', () => {
  it('every row: d_lo within 0.1 m, d_hi within 1 %, same reason', () => {
    const sensors: Record<string, () => SensorP> = {
      eo_1080p_48mm: () => gx40('1080p'), nir_builtin: () => gx40('1080p', { night: true }), nir_ill_l: () => gx40('1080p', { night: true, ext: true }),
      lwir_75mm: () => lwir640(75), lwir_100mm: () => lwir640(100),
    }
    for (const row of FIX.feasibility) {
      const T = row.class === 'vehicle' ? targetFromClass('vehicle', { moving: true, speedMaxMps: 15 }) : targetFromClass(row.class)
      const r = feasibilityDetail({ target: T, sensor: sensors[row.sensor](), light: row.light, morM: row.mor_m })
      if (row.d_hi_m === null) {
        expect(r.dHiM, `${row.class} ${row.sensor} ${row.mor_m}`).toBeNull()
        expect(r.why).toBe(row.why)
      } else {
        expect(r.dLoM).toBeCloseTo(row.d_lo_m ?? NaN, 1)
        expect(close(r.dHiM ?? 0, row.d_hi_m, 0.01, 1)).toBe(true)
      }
    }
  })
  it('window narrower than 10 m is infeasible', () => {
    const T = targetFromClass('person')
    expect(feasibilityWindow({ target: T, sensor: lwir640(75), light: 'night', morM: 10000 })).toBeNull()
    expect(feasibilityWindow({ target: T, sensor: gx40('1080p'), light: 'day', morM: 10000 })).not.toBeNull()
  })
})

describe('reference ranges vs Python (AWR-20 §6.9)', () => {
  it('D, R, I slant ranges within 1 %', () => {
    const REF = {
      person: { size: [0.5, 0.3, 1.75] as const, d: 0.7246, c0: 0.4, rho: 0.45, dt: 8, em: 0.97, hot: 1 },
      vehicle: { size: [4.5, 1.8, 1.5] as const, d: 1.643, c0: 0.5, rho: 0.5, dt: 50, em: 0.9, hot: 0.2 },
    }
    for (const ref of FIX.ref_ranges) {
      const sn = sensorOf(ref.sensor)
      for (const row of ref.rows) {
        const x = REF[row.target as 'person' | 'vehicle']
        const T: TargetP = { size: x.size, contrast0: x.c0, camouflage: 0, reflect850: x.rho, occlusion: 0, criticalDimM: x.d, levelReq: 'R', nReqPx: null,
          moving: false, speedMaxMps: 0, bodies: [], thermal: { mode: 'relative', deltaTK: x.dt, tSurfaceC: 30, emissivity: x.em, hotFraction: x.hot } }
        const env: EnvP = { ...defaultEnv(row.light as 'day' | 'night', row.mor_m), illumLx: row.light === 'day' ? 1e5 : 0.001, rhoVGm3: 10 }
        for (const lv of ['D', 'R', 'I'] as const) {
          const got = maxRangeForLevel(sn, T, env, lv)
          const want = row[`${lv}_m`]
          expect(close(got, want, 0.01, 0.5), `${ref.key} ${row.target} ${row.mor_m} ${lv}: ${got} vs ${want}`).toBe(true)
        }
      }
    }
  })
})

describe('optics anchors (M19-AC-001, AC-002, AC-003)', () => {
  it('GX40 FOV within 0.2 deg of the specification', () => {
    const s = gx40('4k')
    const deg = (r: number) => (r * 180) / Math.PI
    expect(Math.abs(deg(hfovRad(s, 4.8)) - 60.2)).toBeLessThanOrEqual(0.2)
    expect(Math.abs(deg(hfovRad(s, 48)) - 6.6)).toBeLessThanOrEqual(0.2)
    expect(Math.abs(deg(vfovRad(s, 4.8)) - 36.1)).toBeLessThanOrEqual(0.2)
    expect(Math.abs(deg(vfovRad(s, 48)) - 3.7)).toBeLessThanOrEqual(0.2)
  })
  it("p' diffraction only bounds 4K; GSD = R p' / f; anchors 857 / 458 / 1412 / 431 m", () => {
    expect(pPrimeUm(gx40('4k'), 48)).toBeCloseTo(1.76, 3)
    expect(pPrimeUm(gx40('1080p'), 48)).toBeCloseTo(2.9, 6)
    expect(gsdM(300, 2.9, 48)).toBeCloseTo(0.018125, 6)
    const r = (d: number, f: number, p: number) => (d * f) / (14 * p * 1e-3)
    expect(close(r(0.7246, 48, 2.9), 857)).toBe(true)
    expect(close(r(0.3873, 48, 2.9), 458)).toBe(true)
    expect(close(r(0.7246, 48, 1.76), 1412)).toBe(true)
    expect(close(r(0.7246, 100, 12), 431)).toBe(true)
    expect(pixelsOnTarget(0.7246, 48, 857, 2.9)).toBeCloseTo(14, 1)
  })
  it('TTPF: P(1.75 N50) = 0.900; levels', () => {
    expect(ttpf(1.75 * 8, 8)).toBeCloseTo(0.9, 3)
    expect(levelOf(3.4)).toBe(0)
    expect(levelOf(3.5)).toBe(1)
    expect(levelOf(14)).toBe(2)
    expect(levelOf(22.4)).toBe(3)
    expect(N_P90.R).toBe(14)
  })
  it('zoom for a level and the band illuminance', () => {
    expect(zoomForLevel(gx40('1080p'), 100, 0.7246, 14 * 1.3)).toBeCloseTo((18.2 * 100 * 2.9e-3) / 0.7246, 6)
    expect(BAND_LUX.night).toBe(0.001)
  })
})

describe('UI hints (M15-FR-211 item 2)', () => {
  it('sensor summary of the clone form and the zoom slider GSD', () => {
    const s = sensorSummary(gx40('1080p'))
    expect(s.gsdAt100M).toBeCloseTo((100 * 2.9e-3) / 48, 6)
    expect(close(s.maxRangeM.R, 845, 0.01, 1)).toBe(true)
    expect(s.window.day?.dLoM).toBeCloseTo(320.1, 1)
    expect(gsdAtRange(gx40('1080p'), 4.8, 100)).toBeCloseTo(0.0604, 3)
  })
  it('catalog items map to the same sensors', () => {
    const view = JSON.parse(readFileSync(join(import.meta.dirname, '../../../../python/awr/sim/catalog/rest_view.json'), 'utf8'))
    const items = view.sensors.items as { sensor_type: string; kind: string; spec: Record<string, unknown> }[]
    const eo = sensorFromCatalog(items.find((x) => x.sensor_type === 'eo_gx40')!)!
    const ref = gx40('1080p')
    expect(eo.pixelUm).toBe(ref.pixelUm)
    expect(pPrimeUm(eo, 48)).toBeCloseTo(pPrimeUm(ref, 48), 9)
    expect(hfovRad(eo, 4.8, '4k')).toBeCloseTo(hfovRad(ref, 4.8, '4k'), 9)
    const lw = sensorFromCatalog(items.find((x) => x.sensor_type === 'lwir640')!, { lensMm: 75 })!
    expect(lw.fMaxMm).toBe(75)
    expect(sensorFromCatalog(items.find((x) => x.kind === 'radar')!)).toBeNull()
  })
})
