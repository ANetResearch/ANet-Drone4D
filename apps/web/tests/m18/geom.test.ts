// M18-AC-003, AC-014, AC-019 (TS part): critical dimension anchors and the WP-01 fixture; keepout volumes identical to the
// Python reference (apps/web/tests/m18/keepout_cases.json, written by tests/targets/test_keepout.py); inside test, signed
// distance; 3 s entry prediction of manual flight (within 0.2 s of the analytic time).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  bodyVolumes, criticalDim, insideVolume, keepoutVolumes, predictEntry, signedDistance, volumeToWire, type SensitivityBody,
} from '@/engine/targets'

const ROOT = join(import.meta.dirname, '../../../..')
const D = Math.PI / 180

describe('critical dimension (M18-AC-003)', () => {
  it('anchors within 1 %', () => {
    const near = (a: number, b: number) => expect(Math.abs(a - b) / b).toBeLessThanOrEqual(0.01)
    near(criticalDim(0.5, 0.3, 1.75, 0, 0, true), 0.7246)
    near(criticalDim(0.5, 0.3, 1.75, Math.PI / 2, 0), 0.3873)
    near(criticalDim(4.5, 1.8, 1.5, 0, 0), 1.643)
    near(criticalDim(4.5, 1.8, 1.5, 0, Math.PI / 2), 2.598)
    near(criticalDim(4.5, 1.8, 1.5, Math.PI / 2, 0), 2.846)
  })
  it('WP-01 fixture dc_cases', () => {
    const f = JSON.parse(readFileSync(join(ROOT, 'packages/contracts/fixtures/targets/dc_cases.json'), 'utf8')) as {
      cases: { size_m: number[]; eps_deg: number; beta_deg: number; d_c_m: number; d_c_worst_m: number }[]
    }
    for (const c of f.cases) {
      const [l, w, h] = c.size_m
      expect(criticalDim(l, w, h, c.eps_deg * D, c.beta_deg * D)).toBeCloseTo(c.d_c_m, 6)
      expect(criticalDim(l, w, h, c.eps_deg * D, c.beta_deg * D, true)).toBeCloseTo(c.d_c_worst_m, 6)
    }
  })
})

describe('keepout volumes (M18-AC-014)', () => {
  it('match the Python reference', () => {
    const f = JSON.parse(readFileSync(join(import.meta.dirname, 'keepout_cases.json'), 'utf8')) as {
      cases: { cfg: { class: string; motion: { mode?: string }; sensitivity: SensitivityBody[] }; pos: number[]; yaw_rad: number; speed_mps: number;
        margin_m: number; volumes: Record<string, unknown>[] }[]
    }
    expect(f.cases.length).toBeGreaterThanOrEqual(6)
    for (const c of f.cases) {
      const moving = (c.cfg.motion.mode ?? 'static') !== 'static' || c.speed_mps > 0.1
      const vols = keepoutVolumes(c.cfg.sensitivity, c.pos as [number, number, number], c.yaw_rad, moving, c.margin_m)
      expect(vols.length).toBe(c.volumes.length)
      vols.forEach((v, i) => {
        const got = volumeToWire(v, `${c.cfg.class}/${v.volIdx}`)
        const want = c.volumes[i]
        for (const k of Object.keys(want)) {
          const a = got[k], b = want[k]
          if (typeof b === 'number') expect(a as number).toBeCloseTo(b, 6)
          else expect(a).toEqual(b)
        }
      })
    }
  })
})

describe('inside, signed distance, entry prediction (M18-AC-019 front end)', () => {
  const sector: SensitivityBody = { kind: 'visual', shape: 'sector', radius_max_m: 100, z_min_m: 0, z_max_m: 50, half_angle_deg: 30 }
  it('inside and sector direction', () => {
    const [v] = bodyVolumes([sector], [0, 0, 0], Math.PI / 2)
    expect(insideVolume(v, 0, 50, 5)).toBe(true)
    expect(insideVolume(v, 50, 0, 5)).toBe(false)
    expect(insideVolume(v, 0, 50, 60)).toBe(false)
    const [c] = bodyVolumes([sector], [0, 0, 0], Math.PI / 2, 0, true)
    expect(c.shape).toBe('cylinder')
    expect(insideVolume(c, 50, 0, 5)).toBe(true)
  })
  it('signed distance', () => {
    const [cyl] = bodyVolumes([{ kind: 'visual', shape: 'cylinder', radius_max_m: 50, z_min_m: 0, z_max_m: 100 }], [0, 0, 0], 0)
    const r = signedDistance(cyl, 70, 0, 10)
    expect(r.d).toBeCloseTo(20, 9)
    expect(r.n).toEqual([1, 0, 0])
    const [sph] = bodyVolumes([{ kind: 'acoustic', shape: 'sphere', radius_max_m: 50 }], [0, 0, 0], 0)
    expect(signedDistance(sph, 0, 0, 80).d).toBeCloseTo(30, 9)
  })
  it('predicts entry 3 s ahead within 0.2 s', () => {
    const vols = bodyVolumes([{ kind: 'visual', shape: 'cylinder', radius_max_m: 50, z_min_m: 0, z_max_m: 100 }], [0, 0, 0], 0)
    for (const [x0, vx] of [[-80, 12], [-70, 8], [-56, 4]] as const) {
      const t = predictEntry([x0, 0, 20], [vx, 0, 0], vols)
      const want = (-50 - x0) / vx
      if (want <= 3) expect(Math.abs((t ?? 99) - want)).toBeLessThanOrEqual(0.2)
      else expect(t).toBeNull()
    }
    expect(predictEntry([0, 0, 20], [1, 0, 0], vols)).toBe(0)
    expect(predictEntry([-100, 0, 20], [-5, 0, 0], vols)).toBeNull()
  })
})
