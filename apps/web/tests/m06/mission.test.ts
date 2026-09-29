// M06-AC-034 in Node (AWR-15 §10.6; AWR-14 §6.7): mission paths split at the executed index (thin solid g500 /
// planned dashed g300), area patches triangulated on the ground + 0.3 m with 5 % / 10 % fill, waypoint and slot glyphs,
// and the GoTo marker states: preview, accepted, running, succeeded (held 1.5 s, then faded over --duration-quick),
// failed (red only when the viewport red is free), plus the store mapping of the mission layer adapter.
import { describe, expect, it } from 'vitest'
import { ctx as frameCtx, GlyphLayer, GLYPH, GotoMarker, MissionOverlay, Palette, Shape } from '@/engine'
import { INPUT } from '@/lib/tokens/input.gen'
import { MOTION } from '@/lib/tokens/motion.gen'
import { missionDataOf } from '@/viewport/layers/mission'

const shapes = (g: GlyphLayer): number[] => {
  const d = (g as unknown as { data: Float32Array }).data
  return Array.from({ length: g.n }, (_, i) => d[8 * i + 4])
}
const palettes = (g: GlyphLayer): number[] => {
  const d = (g as unknown as { data: Float32Array }).data
  return Array.from({ length: g.n }, (_, i) => d[8 * i + 6])
}

describe('mission overlay and GoTo marker (M06-AC-034)', () => {
  it('stride-4 paths (x, y, z, t) give the same segments as xyz triples', () => {
    const a = new MissionOverlay()
    const b = new MissionOverlay()
    const xyz = [0, 0, 10, 10, 0, 10, 20, 0, 10]
    const xyzt = [0, 0, 10, 0, 10, 0, 10, 1, 20, 0, 10, 2]
    a.setData({ paths: [{ vehicle: 'u', pts: new Float32Array(xyz), doneIdx: 1 }], waypoints: [], areas: [], slots: [], targets: [] })
    b.setData({ paths: [{ vehicle: 'u', pts: new Float32Array(xyzt), doneIdx: 1, stride: 4 }], waypoints: [], areas: [], slots: [], targets: [] })
    const g = new GlyphLayer(GLYPH.capS)
    for (const m of [a, b]) m.update({ ...frameCtx, nowMs: 1000, dpr: 0.5 }, g, true)
    expect([a.thin.n, a.planned.n]).toEqual([1, 1])
    expect([b.thin.n, b.planned.n]).toEqual([1, 1])
    a.dispose()
    b.dispose()
  })

  it('paths, areas and glyphs', () => {
    const m = new MissionOverlay()
    m.setGround(() => 5)
    m.setData({
      paths: [{ vehicle: 'uav1', pts: new Float32Array([0, 0, 10, 10, 0, 10, 20, 0, 10, 30, 0, 10]), doneIdx: 1 }],
      waypoints: [{ x: 0, y: 0, z: 10, state: 'reached' }, { x: 20, y: 0, z: 10, state: 'current' }, { x: 30, y: 0, z: 10, state: 'planned' }],
      areas: [{ ring: [0, 0, 50, 0, 50, 50, 0, 50], kind: 'task' }],
      slots: [{ x: 1, y: 1, z: 1 }], targets: [],
    })
    const g = new GlyphLayer(GLYPH.capS)
    g.begin()
    m.update({ ...frameCtx, nowMs: 1000, dpr: 0.5 }, g, true)
    g.commit(0.5, 640, 360)
    // 1 executed segment (thin) + 4 dashed area outline segments; 2 planned segments (wide dashed)
    expect(m.thin.n).toBe(5)
    expect(m.planned.n).toBe(2)
    expect(m.patch.visible).toBe(true)
    const pa = m.patch.geometry.getAttribute('patchAlpha').array as Float32Array
    expect(pa[0]).toBeCloseTo(0.05, 6)
    const pz = m.patch.geometry.getAttribute('position').array as Float32Array
    expect(pz[2]).toBeCloseTo(5.3, 5)
    expect(shapes(g)).toEqual([Shape.Disc, Shape.Disc, Shape.Ring, Shape.Ring, Shape.Diamond])
    expect(palettes(g)).toEqual([Palette.G400, Palette.G50, Palette.G50, Palette.G50, Palette.G200])
    expect(m.drawCount()).toBe(3)
    m.dispose()
  })

  it('GoTo marker states', () => {
    const m = new MissionOverlay()
    const g = new GlyphLayer(GLYPH.capS)
    const at = (now: number, red = true): number[] => {
      g.begin()
      m.update({ ...frameCtx, nowMs: now, dpr: 0.5 }, g, red)
      g.commit(0.5, 640, 360)
      return shapes(g)
    }
    m.goto.set([0, 0, 0], [0, 0, 10], 'preview', 0)
    expect(at(10)).toEqual([Shape.Goto])
    expect(m.thin.n).toBe(1) // plumb line
    m.goto.setState('accepted', 20)
    expect(at(30)).toEqual([Shape.Ring])
    m.goto.setState('running', 40)
    expect(at(50)).toEqual([Shape.Ring, Shape.Disc])
    m.goto.setState('succeeded', 100)
    expect(at(100 + INPUT.resultHoldMs - 10).length).toBe(2)
    at(100 + INPUT.resultHoldMs + MOTION.durationQuickMs / 2)
    expect(m.goto.alpha).toBeGreaterThan(0)
    expect(m.goto.alpha).toBeLessThan(1)
    expect(at(100 + INPUT.resultHoldMs + MOTION.durationQuickMs + 5)).toEqual([])
    expect(m.goto.state).toBeNull()
    m.goto.set([0, 0, 0], [0, 0, 10], 'failed', 200)
    expect(at(210, true)).toEqual([Shape.Ring, Shape.Triangle])
    expect(palettes(g)).toEqual([Palette.R500, Palette.R500])
    at(220, false)
    expect(palettes(g)).toEqual([Palette.G50, Palette.G50])
    m.dispose()
    expect(new GotoMarker().visible).toBe(false)
  })

  it('stores/mission mapping: executed index from the track item', () => {
    const d = missionDataOf({
      rows: new Map([['m1', { mid: 'm1', generator: 'g', state: 'RUNNING', vehicles: ['uav1'], progressPct: 10, etaS: null, revision: 1, tNs: 0,
        tracks: [{ vehicleId: 'uav1', state: 'ACTIVE', item: 2, total: 5 }] }]]),
      paths: new Map([['uav1', { trajId: 1, rev: 1, pts: new Float32Array(12) }]]), detail: new Map(), coverage: new Map(), preview: null,
    } as never)
    // stores/mission paths are (x, y, z, t_rel_s) quads (polyline4)
    expect(d.paths).toEqual([{ vehicle: 'uav1', pts: expect.any(Float32Array), doneIdx: 2, stride: 4 }])
  })
})
