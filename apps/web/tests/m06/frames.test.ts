// M06-AC-023 (AWR-03 §5.1; M06 §6.8): ENU (E, N, U) under WorldRoot is three (E, U, -N); the camera helpers agree with
// the single frame implementation engine/geo/frames.ts (M02) within the mixed tolerance; the six cities stay within the
// 10 km float32 radius (no M06-E016).
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { Vector3 } from 'three'
import { enuToThree, enuToThreeInto, threeToEnu, threeToEnuInto, WORLD_ROOT } from '@/engine'

const ROOT = join(import.meta.dirname, '../../../..')
const CITIES = ['shenzhen', 'newyork', 'shanghai', 'suzhou', 'sanfrancisco', 'chicago']
// The six cities are built locally from UrbanScene3D (never committed); a clean checkout skips the radius check (SHOW-CI)
const BUILT = CITIES.every((c) => existsSync(join(ROOT, 'worlds', c, 'world.json')))

describe('frames (M06-AC-023)', () => {
  it('matches frames.ts and the WorldRoot matrix on random points (atol 1e-6 m)', () => {
    let s = 12345
    const rnd = (): number => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296 - 0.5) * 8000
    const a = new Float64Array(3)
    const b = new Float64Array(3)
    const v = new Vector3()
    for (let k = 0; k < 1000; k++) {
      const e = rnd(), n = rnd(), u = rnd() / 10
      enuToThree(e, n, u, v)
      enuToThreeInto(a, e, n, u)
      expect(Math.abs(v.x - a[0]) + Math.abs(v.y - a[1]) + Math.abs(v.z - a[2])).toBeLessThan(1e-6)
      const w = new Vector3(e, n, u).applyMatrix4(WORLD_ROOT)
      expect(w.distanceTo(v)).toBeLessThan(1e-6)
      threeToEnu(v, b)
      threeToEnuInto(a, v.x, v.y, v.z)
      expect(Math.abs(b[0] - e) + Math.abs(b[1] - n) + Math.abs(b[2] - u)).toBeLessThan(1e-6)
      expect(Math.abs(a[0] - e) + Math.abs(a[1] - n) + Math.abs(a[2] - u)).toBeLessThan(1e-6)
    }
  })

  it.skipIf(!BUILT)('six cities are within the 10 km horizontal radius (float32 ENU <= 0.69 mm)', () => {
    for (const c of CITIES) {
      const w = JSON.parse(readFileSync(join(ROOT, 'worlds', c, 'world.json'), 'utf8')) as { bounds: { min: number[]; max: number[] } }
      const r = Math.max(Math.hypot(w.bounds.min[0], w.bounds.min[1]), Math.hypot(w.bounds.max[0], w.bounds.max[1]))
      expect(r, c).toBeLessThanOrEqual(10_000)
      // float32 spacing at that radius
      const f = new Float32Array([r])
      expect(Math.abs(f[0] - r)).toBeLessThan(1e-3)
    }
  })
})
