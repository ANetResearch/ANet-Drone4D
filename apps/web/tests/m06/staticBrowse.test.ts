// M06-FR-030 static browsing in Node: while the route's world differs from the session's world the drone layer is
// suppressed (no markers, glyphs, models or selected trails); lifting it restores the vehicles on the next frame.
import { describe, expect, it } from 'vitest'
import { ctx as frameCtx, DroneLayer, InterpRing, type RenderBackendView } from '@/engine'
import { GLPointsNodeMaterial } from '@/viewport/glPointsNodeMaterial'

const be = { tier: 'S', deviceClass: 'software', kind: 'webgl2', pointSizeMode: 'glpoint', startRung: 0, lowestAllowedRung: 0, caps: {},
  createPointsMaterial: () => new GLPointsNodeMaterial(), programsCount: () => 0 } as unknown as RenderBackendView

describe('static browsing (M06-FR-030)', () => {
  it('suppressed layer draws nothing; lifting it restores the vehicles', () => {
    const interp = new InterpRing()
    const ids = new Map([[1, { id: 'a', model: 'p600', kind: 'uav' }], [2, { id: 'b', model: 'p600', kind: 'uav' }]])
    for (const a of [1, 2]) {
      const s = interp.slotFor(a)
      interp.push(s, 0, a * 10, 0, 20, 0, 0, 0, 0, 0, 0, 1, 0)
      interp.push(s, 100, a * 10, 0, 20, 0, 0, 0, 0, 0, 0, 1, 0)
    }
    interp.eMaxMs = 1e9
    const roster = { get size() { return ids.size }, get: (a: number) => ids.get(a), idOf: (a: number) => ids.get(a)?.id }
    const layer = new DroneLayer({ be, interp, roster: () => roster, subscribe: () => () => {} })
    const ctx = { ...frameCtx, cssW: 1280, cssH: 720, dbW: 640, dbH: 360, dpr: 0.5, camera: null }
    layer.setHighlights([1], 1, -1)
    layer.update({ ...ctx, nowMs: 50, tRenderS: 0.05 })
    expect(layer.markers.n).toBe(2)
    layer.suppressed = true
    layer.update({ ...ctx, nowMs: 80, tRenderS: 0.08 })
    layer.glyphs.commit(0.5, 640, 360)
    expect([layer.markers.n, layer.glyphs.n, layer.trailSel.drawCount()]).toEqual([0, 0, 0])
    layer.suppressed = false
    layer.update({ ...ctx, nowMs: 110, tRenderS: 0.1 })
    expect(layer.markers.n).toBe(2)
    layer.dispose()
  })
})
