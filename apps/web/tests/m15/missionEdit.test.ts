// Route and area editor model (AWR-14 §6.8; UX-FR-023, UX-FR-024; D1-AC-17; P4-UI): edits, the bounded undo history,
// reference changes that keep the world height, ADR-016 limits, area rings (rectangle, self-intersection, generator
// parameters) and the viewport hit and drag geometry.
import { describe, expect, it } from 'vitest'
import {
  canAdd, centroid, generatorParams, insertAt, midpoint, nearestMidpoint, nearestScreen, polygonArea, pushHistory, rayPlaneZ, rayVerticalZ,
  rectFrom, redo, removeAt, reorder, ROUTE_LIMITS, routeLength, routePoints, sameRoute, selfIntersects, undo, updateAt, withRef, worldZ,
  type DraftWp, type History,
} from '@/ui/panels/mission-edit/editModel'

const flat = (z: number) => () => z
// fixture ids far from the ids newWpId() hands out
const wp = (id: number, x: number, y: number, h = 20, ref: DraftWp['ref'] = 'AGL'): DraftWp => ({ id: 100_000 + id, x, y, h, ref })

describe('route edits', () => {
  it('inserts, updates, removes and reorders without touching other waypoints', () => {
    let w: DraftWp[] = [wp(1, 0, 0), wp(2, 10, 0)]
    w = insertAt(w, 1, { x: 5, y: 5, h: 30, ref: 'WORLD' })
    expect(w.map((p) => [p.x, p.y])).toEqual([[0, 0], [5, 5], [10, 0]])
    expect(new Set(w.map((p) => p.id)).size).toBe(3)
    w = updateAt(w, 2, { y: 7 })
    expect(w[2]).toMatchObject({ x: 10, y: 7, id: 100_002 })
    w = reorder(w, 0, 1)
    expect(w.map((p) => p.id)[1]).toBe(100_001)
    expect(reorder(w, 0, -1).map((p) => p.id)).toEqual(w.map((p) => p.id))
    w = removeAt(w, 1)
    expect(w.length).toBe(2)
    expect(w.some((p) => p.id === 100_001)).toBe(false)
  })

  it('converts AGL to world z with the terrain and keeps the world height on a reference change', () => {
    const g = (x: number) => 5 + x / 10
    const a = wp(1, 10, 0, 20, 'AGL')
    expect(worldZ(a, g)).toBeCloseTo(26)
    const b = withRef(a, 'WORLD', g)
    expect(b.ref).toBe('WORLD')
    expect(worldZ(b, g)).toBeCloseTo(26)
    expect(withRef(b, 'AGL', g).h).toBeCloseTo(20)
    const pts = routePoints([a, wp(2, 20, 0, 30, 'WORLD')], g)
    expect(Array.from(pts)).toEqual([10, 0, 26, 20, 0, 30])
    expect(routeLength(pts)).toBeCloseTo(Math.hypot(10, 4))
  })

  it('inserts at the midpoint of a segment in the reference of its first waypoint', () => {
    const w = [wp(1, 0, 0, 10, 'AGL'), wp(2, 10, 10, 50, 'WORLD')]
    const m = midpoint(w, 0, flat(0))!
    expect(m).toMatchObject({ x: 5, y: 5, ref: 'AGL' })
    expect(m.h).toBeCloseTo(30)
    expect(midpoint(w, 1, flat(0))).toBeNull()
  })

  it('keeps at most 50 undo steps and redoes what was undone', () => {
    let h: History = { past: [], future: [] }
    let cur: readonly DraftWp[] = []
    for (let i = 0; i < 60; i++) {
      h = pushHistory(h, cur)
      cur = insertAt(cur, cur.length, { x: i, y: 0, h: 10, ref: 'AGL' })
    }
    expect(h.past.length).toBe(ROUTE_LIMITS.historyMax)
    const u = undo(h, cur)!
    expect(u.wps.length).toBe(59)
    const r = redo(u.h, u.wps)!
    expect(r.wps.length).toBe(60)
    expect(redo(r.h, r.wps)).toBeNull()
    expect(sameRoute(r.wps, cur)).toBe(true)
  })

  it('stops adding at 1000 waypoints (ADR-016)', () => {
    expect(canAdd(999)).toBe(true)
    expect(canAdd(1000)).toBe(false)
  })
})

describe('area', () => {
  it('builds a counter-clockwise rectangle from two corners', () => {
    const r = rectFrom([10, 20], [0, 0])
    expect(r).toEqual([[0, 0], [10, 0], [10, 20], [0, 20]])
    expect(polygonArea(r)).toBe(200)
    expect(centroid(r)).toEqual([5, 10])
  })

  it('refuses a self-intersecting ring', () => {
    expect(selfIntersects([[0, 0], [10, 0], [10, 10], [0, 10]])).toBe(false)
    expect(selfIntersects([[0, 0], [10, 10], [10, 0], [0, 10]])).toBe(true)
  })

  it('maps a closed area to M10 lawnmower and expanding square parameters', () => {
    const cw: [number, number][] = [[0, 0], [0, 30], [40, 30], [40, 0]]
    const lm = generatorParams(cw, { generator: 'lawnmower', aglM: 25, spacingM: 15, speedMps: 5 }) as { polygon_enu_m: number[][]; altitude: unknown; spacing_m: number }
    expect(polygonArea(lm.polygon_enu_m as [number, number][])).toBeGreaterThan(0)
    expect(lm.altitude).toEqual({ mode: 'fixed_agl', agl_m: 25 })
    expect(lm.spacing_m).toBe(15)
    const es = generatorParams(cw, { generator: 'expanding_square', aglM: 30, spacingM: 12, speedMps: 4 })
    expect(es).toMatchObject({ datum_enu_m: [20, 15], agl_m: 30, leg0_m: 12, max_extent_m: 20, speed_mps: 4 })
  })
})

describe('viewport geometry', () => {
  it('finds the nearest projected waypoint and segment midpoint within the tolerance', () => {
    const scr = Float32Array.from([100, 100, 200, 100, Number.NaN, Number.NaN])
    expect(nearestScreen(scr, 3, 104, 98, 10)).toBe(0)
    expect(nearestScreen(scr, 3, 150, 100, 10)).toBe(-1)
    expect(nearestMidpoint(scr, 3, 151, 102, 10)).toBe(0)
    expect(nearestMidpoint(scr, 3, 300, 100, 10)).toBe(-1)
  })

  it('intersects a camera ray with a horizontal plane and with a vertical line', () => {
    const o = [0, 0, 100]
    const d = [Math.SQRT1_2, 0, -Math.SQRT1_2]
    expect(rayPlaneZ(o, d, 20)).toEqual([expect.closeTo(80), expect.closeTo(0)])
    expect(rayPlaneZ(o, [1, 0, 0], 20)).toBeNull()
    expect(rayPlaneZ(o, [0, 0, 1], 20)).toBeNull()
    // looking along +E and down 45 degrees: the line at E = 30 is met at height 70
    expect(rayVerticalZ(o, d, 30, 0)).toBeCloseTo(70)
    expect(rayVerticalZ(o, [0, 0, -1], 30, 0)).toBeNull()
  })
})
