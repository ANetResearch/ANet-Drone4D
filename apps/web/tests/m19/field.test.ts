// M19-AC-022 (Node part), M19-NFR-009, M18-AC-025: the field payloads decode as the server encodes them (field_cases.json
// from tests/perception/test_field_golden.py): the interpolated profile value equals the server's exact samples within
// 0.5 dB (dB quantities) or 2 % (linear quantities); `same` references and delta frames reuse the arrays of the previous
// keyframe; the volume table model reproduces the server's explicit samples (azimuth linear, elevation nearest) and
// P(r_eff) = 0.5; the configured-body mask equals Python inside() on 10^4 random points for the four shapes.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  azRel, bodyMask, decodeFieldProfile, decodeFieldVolumes, isoRadius, profileBand, profileValue, tableRef, volumeP, volumeREff,
} from '@/engine/perception'
import type { SensitivityBody } from '@/engine/targets'

interface ProfCase { kind: string; model: string; frame: unknown; samples: [number, number, number | null][]; delta: unknown }
const FIX = JSON.parse(readFileSync(join(import.meta.dirname, 'field_cases.json'), 'utf8')) as {
  profiles: ProfCase[]
  volumes: { frame: unknown; samples: { target: string; body: number; model: number; pos: [number, number, number]; yaw: number; cfg: SensitivityBody;
    points: [number, number, number, number, boolean][] }[] }
  masks: { body: SensitivityBody; pos: [number, number, number]; yaw: number; inside: number[] }[]
  mask_points: number[]
}

describe('field profiles (M19-AC-022)', () => {
  it('interpolation within 0.5 dB or 2 % of the exact samples for every kind', () => {
    expect(FIX.profiles.length).toBe(8)
    for (const c of FIX.profiles) {
      const f = decodeFieldProfile(c.frame)
      expect(f, c.kind).not.toBeNull()
      const u = f!.uavs[0]
      expect(f!.uavs[1].vR).toEqual(u.vR)                 // `same` reference
      let n = 0
      for (const [r, e, want] of c.samples) {
        const got = profileValue(f!, u, r, e)
        if (want === null || Number.isNaN(got)) continue
        n++
        if (f!.combine === 'add') expect(Math.abs(got - want), `${c.kind} ${c.model} r=${r}`).toBeLessThanOrEqual(0.5)
        else expect(Math.abs(got - want) / Math.abs(want), `${c.kind} ${c.model} r=${r}`).toBeLessThanOrEqual(0.02)
      }
      expect(n).toBeGreaterThan(50)
    }
  })

  it('delta frames reuse the arrays of the previous keyframe; unknown kinds and orphan deltas are dropped', () => {
    const c = FIX.profiles[0]
    const key = decodeFieldProfile(c.frame)!
    const d = decodeFieldProfile(c.delta, key)!
    expect(d.uavs.map((u) => u.id)).toEqual(['uav-01', 'uav-02'])
    expect(d.uavs[0]).toBe(key.uavs[0])
    expect(d.rM).toBe(key.rM)
    expect(decodeFieldProfile(c.delta, null)).toBeNull()
    expect(decodeFieldProfile({ kind: 'sonar' })).toBeNull()
  })

  it('bands, legend and iso radius', () => {
    const c = FIX.profiles.find((p) => p.kind === 'acoustic')!
    const f = decodeFieldProfile(c.frame)!
    expect(f.unit).toBe('dba')
    expect(f.bands).toEqual([20, 30, 40, 50, 60])
    expect(profileBand(f, 35)).toBe(2)
    expect(profileBand(f, 65)).toBe(5)
    expect(profileBand(f, Number.NaN)).toBe(0)
    const bg = f.iso.find((x) => x.id === 'bg')!
    const r = isoRadius(f, f.uavs[0], bg.v)
    expect(Math.abs(profileValue(f, f.uavs[0], r, 15) - bg.v)).toBeLessThanOrEqual(0.05)
    expect(f.uavs[0].shellM).toBeGreaterThan(0)
  })
})

describe('perception volumes (M19-AC-023 front-end part)', () => {
  it('table model reproduces the explicit samples and P(r_eff) = 0.5', () => {
    const v = decodeFieldVolumes(FIX.volumes.frame)!
    expect(v.models).toEqual(['awr_f1', 'awr_q9', 'p600_mid360'])
    expect([...v.targets.keys()]).toEqual(['person-01', 'dog-01'])
    for (const s of FIX.volumes.samples) {
      const b = v.targets.get(s.target)!.bodies[s.body]
      const tab = b.rEffM[s.model]
      const ref = tableRef(tab)
      for (const [x, y, z, want] of s.points) {
        const dx = x - s.pos[0], dy = y - s.pos[1], dz = z - s.pos[2]
        const r = Math.hypot(dx, dy, dz)
        const rEff = volumeREff(tab, azRel(dx, dy, s.yaw), Math.asin(dz / r))
        const p = volumeP(r, rEff, b.wM[s.model], ref)
        expect(Math.abs(p - want)).toBeLessThanOrEqual(0.01)
      }
      expect(volumeP(ref, ref, b.wM[s.model], ref)).toBe(0.5)
    }
  })

  it('configured-body mask equals Python inside() on 10^4 points (M18-AC-025)', () => {
    const pts = Float64Array.from(FIX.mask_points)
    expect(pts.length).toBe(30000)
    for (const m of FIX.masks) {
      const out = new Uint8Array(pts.length / 3)
      const n = bodyMask(m.body, m.pos, m.yaw, pts, out)
      expect(Array.from(out)).toEqual(m.inside)
      expect(n).toBe(m.inside.reduce((a, b) => a + b, 0))
    }
  })
})
