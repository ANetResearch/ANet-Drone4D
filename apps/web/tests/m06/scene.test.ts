// M06-AC-020 (ADR-002; M06 §6.7): WorldRoot is unique with rotation.x = -pi/2; the registry sums the pass plan and
// keeps the point cloud on CH_CLOUD; every D1 layer adapter exists and stays within 150 lines (FR-025, FR-016).
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { Group } from 'three'
import { ctx } from '@/engine/loop'
import { channelOf, CH_CLOUD, CH_MAIN, listLayers, plannedDraws, pointCloudServices, registerLayer } from '@/viewport/layers/registry'
import { vp } from '@/viewport/session'

const SRC = join(import.meta.dirname, '../../src/viewport')

describe('scene structure and layer registry (M06-AC-020)', () => {
  it('WorldRoot rotation.x = -pi/2 maps ENU (E, N, U) to three (E, U, -N)', () => {
    expect(vp.worldRoot.name).toBe('WorldRoot')
    expect(vp.worldRoot.rotation.x).toBeCloseTo(-Math.PI / 2, 12)
    const g = new Group()
    g.position.set(10, 20, 30)
    vp.worldRoot.add(g)
    vp.worldRoot.updateMatrixWorld(true)
    const e = g.matrixWorld.elements
    expect([e[12], e[13], e[14]].map((v) => Math.round(v * 1e9) / 1e9)).toEqual([10, 30, -20])
    vp.worldRoot.remove(g)
  })

  it('sums drawCount into the pass plan; the point cloud is always CH_CLOUD; services come from its spec', () => {
    const cas = { state: () => ({ atFloorSinceMs: 0, atCeilSinceMs: 0, B: 0, lo: 0, hi: 0, rung: 0 }), setFloorOverride: () => {} }
    const offs = [
      registerLayer({ id: 'debug', owner: 'M06', perfKey: 'mainJs', root: null, channel: 0, drawCount: () => 2, setVisible: () => {}, dispose: () => {} }),
      registerLayer({ id: 'pointcloud', owner: 'M05', perfKey: 'pointcloud', root: null, channel: 0, drawCount: () => 1, setVisible: () => {}, dispose: () => {}, services: { cas } }),
    ]
    expect(plannedDraws(ctx)).toBe(3)
    const pc = listLayers().find((l) => l.id === 'pointcloud')!
    expect(channelOf(pc)).toBe(CH_CLOUD)
    expect(channelOf(listLayers().find((l) => l.id === 'debug')!)).toBe(CH_MAIN)
    expect(pointCloudServices()?.cas).toBe(cas)
    for (const off of offs) off()
    expect(plannedDraws(ctx)).toBe(0)
    expect(pointCloudServices()).toBeNull()
  })

  it('every M06 layer adapter exists and is <= 150 lines (WorldCanvas <= 150)', () => {
    const files = readdirSync(join(SRC, 'layers')).filter((f) => f.endsWith('.tsx'))
    for (const want of ['drones.tsx', 'trails.tsx', 'glyphs.tsx', 'mission.tsx', 'zones.tsx', 'sensors.tsx', 'groundSky.tsx', 'debug.tsx']) expect(files).toContain(want)
    for (const f of [...files.filter((x) => x !== 'pointcloud.tsx' && x !== 'environment.tsx').map((x) => join(SRC, 'layers', x)), join(SRC, 'WorldCanvas.tsx')]) {
      const n = readFileSync(f, 'utf8').split('\n').length
      expect(n, f).toBeLessThanOrEqual(150)
    }
  })
})
