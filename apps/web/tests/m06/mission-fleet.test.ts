// D2-FX-R2-other (D2-AC-26 -> D1-AC-03b; AWR-03 §3.8): above MISSION.allPathsS / allPathsBA vehicles with paths the
// mission overlay draws only the paths of the trail vehicles (selected, primary, focus-set trail slots), so the ladder
// n200 (200 orbit paths) stays inside the trails layer group budget; fleets at or below the limit are drawn in full.
import { describe, expect, it } from 'vitest'
import { ctx as frameCtx, GlyphLayer, GLYPH, MISSION, MissionOverlay, type DroneLayer, type MissionPath } from '@/engine'
import { trailVehicleIds, trailVehiclesKey } from '@/viewport/layers/mission'

const orbit = (vehicle: string, n = 12): MissionPath => {
  const pts: number[] = []
  for (let i = 0; i <= n; i++) pts.push(3 * Math.cos((2 * Math.PI * i) / n), 3 * Math.sin((2 * Math.PI * i) / n), 60, i)
  return { vehicle, pts: new Float32Array(pts), doneIdx: 0, stride: 4 }
}
const fleet = (k: number): MissionPath[] => Array.from({ length: k }, (_, i) => orbit(`sim-${i}`))
const data = (paths: MissionPath[]) => ({ paths, waypoints: [], areas: [], slots: [], targets: [] })

function run(m: MissionOverlay, nowMs = 1000): void {
  const g = new GlyphLayer(GLYPH.capS)
  g.begin()
  m.update({ ...frameCtx, nowMs, dpr: 0.5 }, g, true)
}

describe('mission overlay vehicle limit for large fleets', () => {
  it('draws every path up to the limit', () => {
    const m = new MissionOverlay()
    m.allPathsMax = MISSION.allPathsS
    m.setPathVehicles(new Set())
    m.setData(data(fleet(MISSION.allPathsS)))
    run(m)
    expect(m.pathStats).toEqual({ drawn: MISSION.allPathsS, skipped: 0 })
    expect(m.planned.n).toBe(MISSION.allPathsS * 12)
    m.dispose()
  })

  it('above the limit draws only the chosen vehicles and rebuilds when they change', () => {
    const m = new MissionOverlay()
    m.allPathsMax = MISSION.allPathsS
    m.setData(data(fleet(200)))
    run(m, 1000)
    expect(m.pathStats).toEqual({ drawn: 0, skipped: 200 }) // no vehicle chosen yet
    expect(m.planned.n).toBe(0)
    m.setPathVehicles(new Set(['sim-3', 'sim-7']))
    run(m, 1300)
    expect(m.pathStats).toEqual({ drawn: 2, skipped: 198 })
    expect(m.planned.n).toBe(24)
    m.dispose()
  })

  it('without a limit (default) every path is drawn, as before', () => {
    const m = new MissionOverlay()
    m.setData(data(fleet(40)))
    run(m)
    expect(m.pathStats.drawn).toBe(40)
    m.dispose()
  })

  it('trail vehicles of the drone layer: selected, primary and focus-set slots', () => {
    const ids = ['a', 'b', 'c', 'd', 'e']
    const slotAgent = new Int32Array([3, -1, 4, -1])
    const L = {
      selected: new Int32Array([1]), primary: 0, trailFocus: { slotAgent },
      o: { roster: () => ({ size: ids.length, idOf: (a: number) => ids[a], get: () => undefined }) },
    } as unknown as DroneLayer
    expect([...trailVehicleIds(L)].sort()).toEqual(['a', 'b', 'd', 'e'])
    const k0 = trailVehiclesKey(L)
    expect(trailVehiclesKey(L)).toBe(k0)
    slotAgent[1] = 2
    expect(trailVehiclesKey(L)).not.toBe(k0)
    expect([...trailVehicleIds(L)].sort()).toEqual(['a', 'b', 'c', 'd', 'e'])
  })
})
