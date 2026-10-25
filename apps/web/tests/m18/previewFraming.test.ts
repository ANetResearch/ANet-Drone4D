import { describe, expect, it } from 'vitest'
import { previewDistance } from '@/engine/targets/previewFraming'
import { TARGET_ASSETS } from '@/engine/targets/assetData'

describe('appearance preview framing', () => {
  it('keeps every asset inside landscape and narrow viewports from front, side and three-quarter views', () => {
    for (const asset of TARGET_ASSETS) for (const aspect of [.7, 1.4, 2.8]) for (const yaw of [0, -.65, Math.PI / 2, Math.PI]) {
      const pitch = .22, cp = Math.cos(pitch), sp = Math.sin(pitch), cy = Math.cos(yaw), sy = Math.sin(yaw)
      const d = previewDistance(asset.dimensions_m, yaw, pitch, aspect)
      for (const x of [-asset.dimensions_m[0] / 2, asset.dimensions_m[0] / 2]) for (const y of [-asset.dimensions_m[1] / 2, asset.dimensions_m[1] / 2]) for (const z of [-asset.dimensions_m[2] / 2, asset.dimensions_m[2] / 2]) {
        const depth = d - (x * cy * cp + y * sy * cp + z * sp)
        expect(depth).toBeGreaterThan(0)
        expect(Math.abs(-x * sy + y * cy) / depth).toBeLessThan(Math.tan(19 * Math.PI / 180) * aspect)
        expect(Math.abs(-x * cy * sp - y * sy * sp + z * cp) / depth).toBeLessThan(Math.tan(19 * Math.PI / 180))
      }
    }
  })
  it('uses available width for long vehicles instead of shrinking them to a tall bounding sphere', () => {
    const size = [12.2, 3.08, 3.43]
    const square = previewDistance(size, Math.PI / 2, .22, 1)
    const wide = previewDistance(size, Math.PI / 2, .22, 2.8)
    expect(wide).toBeLessThan(square / 2)
  })
})
