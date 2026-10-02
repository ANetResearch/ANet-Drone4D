// M06-AC-036 in Node (AWR-16 §7; AWR-15 §10.12): the six cities' zones.geojson parse into awr.zones.v1 features; built
// walls, outlines and edges match the rings; border is not drawn by default; min_z null -> lowest ground, max_z null ->
// world top + 50 m; the violated zone is recoloured through uniforms and attribute updates only (no rebuild).
import { QL_STRIDE, QL_VPS } from '@/engine/lines/quadLines'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { parseZones, pointInRing, ZonesLayer, type ZoneFeature } from '@/engine'

const ROOT = join(import.meta.dirname, '../../../..')

describe('zones (M06-AC-036)', () => {
  it('six cities parse; border hidden by default', () => {
    for (const c of ['shenzhen', 'newyork', 'shanghai', 'suzhou', 'sanfrancisco', 'chicago']) {
      const f = parseZones(JSON.parse(readFileSync(join(ROOT, 'worlds', c, 'semantic/zones.geojson'), 'utf8')))
      expect(f.length, c).toBeGreaterThanOrEqual(1)
      const z = new ZonesLayer('S')
      z.build(f, 0, 300)
      expect(z.zones.some((x) => x.kind === 'border')).toBe(false)
      z.dispose()
    }
  })

  it('geometry matches the rings, z defaults, red zone without rebuild', () => {
    const features: ZoneFeature[] = [
      { id: 'nf1', kind: 'nofly', ring: [[0, 0], [100, 0], [100, 50], [0, 50], [0, 0]], minZ: null, maxZ: 120, label: 'NF' },
      { id: 'r1', kind: 'restricted', ring: [[200, 0], [260, 0], [230, 40]], minZ: 10, maxZ: null, label: 'R' },
    ]
    const z = new ZonesLayer('B')
    z.build(features, -5, 300)
    expect(z.zones.map((x) => [x.id, x.z0, x.z1])).toEqual([['nf1', -5, 120], ['r1', 10, 350]])
    const pos = z.walls.geometry.getAttribute('position').array as Float32Array
    expect(pos.length / 3).toBe((4 + 3) * 6)
    expect([pos[0], pos[1], pos[2]]).toEqual([0, 0, -5])
    expect(z.topSolid.n).toBe(4)
    expect(z.topDashed.n).toBe(3)
    expect(z.edges.n).toBe(7)
    expect(z.drawCount()).toBe(4)
    const geomBefore = z.walls.geometry.getAttribute('position')
    z.setHero('r1')
    // quad-line layout (FX2-R3): palette index at float 8 of every vertex, 4 vertices per segment
    const data = (z.topDashed as unknown as { data: Float32Array }).data
    const col = (): number[] => [0, 1, 2].map((i) => data[i * QL_VPS * QL_STRIDE + 8])
    expect(col()).toEqual([1, 1, 1]) // r500 palette index
    expect(z.walls.geometry.getAttribute('position')).toBe(geomBefore)
    z.setHero(null)
    expect(col()).toEqual([4, 4, 4])
    expect(z.zoneAt(50, 25, 60)?.id).toBe('nf1')
    expect(z.zoneAt(50, 25, 200)).toBeNull()
    expect(pointInRing(new Float64Array([0, 0, 10, 0, 10, 10, 0, 10]), 5, 5)).toBe(true)
    z.dispose()
  })
})
