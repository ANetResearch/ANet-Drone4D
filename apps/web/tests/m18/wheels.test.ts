import { describe, expect, it } from 'vitest'
import { rollWheels } from '../../src/engine/targets/wheels'

describe('distance-driven source wheels', () => {
  it('preserves the body and rolls the wheel without mutating shared sources', () => {
    const base = new Float32Array([3, 2, 4, 1, 0, 1])
    const normals = new Float32Array([1, 0, 0, 1, 0, 0])
    const out = new Float32Array(6), n = new Float32Array(6)
    const groups = { ids: new Int32Array([-1, 0]), centers: new Float32Array([0, 0, 1]), radii: new Float32Array([1]) }
    rollWheels(base, normals, out, n, groups, Math.PI / 2)
    expect(Array.from(out.slice(0, 3))).toEqual([3, 2, 4])
    expect(out[3]).toBeCloseTo(0); expect(out[5]).toBeCloseTo(0)
    expect(n[3]).toBeCloseTo(0); expect(n[5]).toBeCloseTo(-1)
    expect(Array.from(base)).toEqual([3, 2, 4, 1, 0, 1])
    rollWheels(base, normals, out, n, groups, Math.PI * 2)
    expect(out[3]).toBeCloseTo(1); expect(out[5]).toBeCloseTo(1)
  })
})
