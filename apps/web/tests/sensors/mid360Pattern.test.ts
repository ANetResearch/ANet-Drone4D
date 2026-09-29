// M13-AC-024 (TS side): table-driven MID-360 pattern against the r04 reference samples of tests/sensors/golden/mid360.json.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { MID360, mid360Pattern } from '@/engine/sensors/mid360Pattern'

const gold = JSON.parse(readFileSync(new URL('../../../../tests/sensors/golden/mid360.json', import.meta.url), 'utf8')) as {
  frames: { frame: number; idx: number[]; az_deg: number[]; el_deg: number[] }[]
}

describe('mid360Pattern', () => {
  it('matches the reference within 1e-6 deg on frames 0, 1 and 987654', () => {
    const az = new Float64Array(MID360.NPF)
    const el = new Float64Array(MID360.NPF)
    for (const f of gold.frames) {
      mid360Pattern(f.frame, az, el)
      let ea = 0
      let ee = 0
      f.idx.forEach((i, k) => {
        const d = ((az[i] - f.az_deg[k] + 540) % 360) - 180
        ea = Math.max(ea, Math.abs(d))
        ee = Math.max(ee, Math.abs(el[i] - f.el_deg[k]))
      })
      expect(ea).toBeLessThanOrEqual(1e-6)
      expect(ee).toBeLessThanOrEqual(1e-6)
      let lo = Infinity
      let hi = -Infinity
      for (let i = 0; i < el.length; i++) {
        lo = Math.min(lo, el[i])
        hi = Math.max(hi, el[i])
        expect(az[i] >= 0 && az[i] < 360).toBe(true)
      }
      expect(lo).toBeGreaterThanOrEqual(MID360.FOV_EL_MIN_DEG)
      expect(hi).toBeLessThanOrEqual(MID360.FOV_EL_MAX_DEG)
    }
  })
  it('accepts Float32Array outputs', () => {
    const az = new Float32Array(MID360.NPF)
    const el = new Float32Array(MID360.NPF)
    mid360Pattern(5, az, el)
    expect(Number.isFinite(az[19999])).toBe(true)
  })
})
