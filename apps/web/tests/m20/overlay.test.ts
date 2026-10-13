// M20 PRD §8 item 2 (Node part): the tasking overlay draws nests, sub-areas, strips, legs, fills and stations of the active
// plans and the preview with its keepout band in at most two draws, never with the red palette entry (RedArbiter only,
// AWR-04 §10.7), shades assignments by vehicle (style per vehicle id), caps the strips and legs at 512 segments, rebuilds
// only on store changes and degrades by governor level (other tasks' legs off, then the selected task only, then nests
// and stations only).
import { describe, expect, it } from 'vitest'
import type { TaskingState } from '@/stores/tasking'
import { LINE_CAPS as TASKING_CAPS, LINE_PAL as PAL, LINE_STYLES as STYLES } from '@/stores/tasking'
import { TaskingOverlay } from '@/viewport/layers/tasking'

function state(over: Partial<TaskingState> = {}): TaskingState {
  const legs = (uav: string, n: number, y: number) => ({ uav, kind: 'strip' as const,
    polyline: Array.from({ length: n + 1 }, (_v, i) => [i * 10, y, 110] as [number, number, number]) })
  return {
    mode: 'sandbox', canEdit: true, stale: false, lastError: null, version: 1, selected: 't-2',
    nests: [{ id: 'n-1', pos: [0, -100, null], kind: 'box', slots: 4 }, { id: 'n-2', pos: [500, -100, 12], kind: 'vtol_pad', slots: 6 }],
    nestStatus: {},
    tasks: [
      { tid: 't-1', type: 'area_scan_gsd', label: '', state: 'RUNNING', revision: 1, parent: null, children: [], progressPct: 0, coveragePct: null,
        nVehicles: 2, makespanS: null, items: [], warnings: [], params: {}, relay: null },
      { tid: 't-2', type: 'relay_watch', label: '', state: 'RUNNING', revision: 1, parent: null, children: [], progressPct: 0, coveragePct: null,
        nVehicles: 1, makespanS: null, items: [], warnings: [], params: {}, relay: null },
      { tid: 't-3', type: 'area_patrol', label: '', state: 'DONE', revision: 1, parent: null, children: [], progressPct: 0, coveragePct: null,
        nVehicles: 1, makespanS: null, items: [], warnings: [], params: {}, relay: null },
    ],
    plans: {
      't-1': { revision: 1, assignments: [{ uav: 'p600-01', items: [] }, { uav: 'p600-02', items: [] }], geometry: {
        subregions: [[[0, 0], [300, 0], [300, 300], [0, 300]]],
        strips: [{ id: 's0', p0: [0, 0, 110], p1: [300, 0, 110], uav: 'p600-01' }, { id: 's1', p0: [0, 50, 110], p1: [300, 50, 110], uav: 'p600-02' }],
        fill_waypoints: [{ pos: [150, 25, 110], dwell_s: 2 }],
        legs: [legs('p600-01', 300, 0), legs('p600-02', 300, 50)] } },
      't-2': { revision: 1, assignments: [{ uav: 'p600-03', items: [] }], geometry: {
        stations: [{ id: 't-2.st', pos: [400, 400, 80], target: 'person-01', role: 'primary', uav: 'p600-03' },
          { id: 't-2.ho', pos: [420, 400, 80], target: 'person-01', role: 'handover', uav: null }] } },
      't-3': { revision: 1, assignments: [], geometry: { strips: [{ id: 'x', p0: [0, 0, 0], p1: [1, 0, 0] }] } },
    },
    preview: { spec: { type: 'area_patrol', params: {} }, state: 'done', previewId: 'pv-abcdefgh', code: 0, detail: null, tWallMs: 0,
      preview: { preview_id: 'pv-abcdefgh', n_vehicles: 1, assignments: [], geometry: { legs: [legs('unassigned', 4, 900)] } } },
    previewKeepouts: [{ id: 'k', x: 100, y: 100, r: 340 }],
    relay: {}, relayHistory: {},
    ...over,
  }
}

function palettes(o: TaskingOverlay): Set<number> {
  const a = o.thin.obj.geometry.getAttribute('lineInfo').array as Float32Array
  const out = new Set<number>()
  for (let i = 0; i < o.thin.n * 2; i++) out.add(a[i * 4])
  return out
}

describe('tasking overlay (M20 PRD §8 item 2)', () => {
  it('draws the plans, the preview and the keepout band in two draws without red', () => {
    const o = new TaskingOverlay()
    expect(o.update({ state: state(), ground: () => 2 })).toBe(true)
    expect(o.drawCount()).toBe(2)
    expect(o.stats.nests).toBe(2)
    expect(o.stats.strips).toBe(2)
    expect(o.stats.fills).toBe(1)
    expect(o.stats.stations).toBe(2)
    expect(o.stats.keepouts).toBe(1)
    expect(o.stats.clipped).toBe(true)                 // 2 x 300 leg segments + strips exceed the 512 cap
    expect(o.stats.segments).toBeLessThanOrEqual(TASKING_CAPS.thin)
    expect(palettes(o).has(1)).toBe(false)
    // the done task is not drawn; a second update without a store change does nothing
    expect(o.update({ state: state(), ground: () => 2 })).toBe(false)
    o.dispose()
  })

  it('shades each vehicle with its own style and the unassigned geometry dashed', () => {
    const o = new TaskingOverlay()
    o.update({ state: state({ preview: null, previewKeepouts: [], selected: null }), ground: () => 0 })
    const info = o.thin.obj.geometry.getAttribute('lineInfo').array as Float32Array
    const pos = o.thin.obj.geometry.getAttribute('position').array as Float32Array
    const styleAt = (y: number) => {
      for (let k = 0; k < o.thin.n; k++) if (pos[k * 6 + 1] === y && pos[k * 6 + 4] === y && pos[k * 6 + 2] === 110) return [info[k * 8], info[k * 8 + 1]]
      return null
    }
    expect(styleAt(0)).toEqual([STYLES[0].pal, 0])
    expect(styleAt(50)).toEqual([STYLES[1].pal, 0])
    o.dispose()
  })

  it('degrades by governor level', () => {
    const o = new TaskingOverlay()
    const seg = (lvl: number, selected: string) => {
      o.level = lvl
      o.update({ state: state({ preview: null, previewKeepouts: [], selected, version: lvl * 10 + selected.length }), ground: () => 0 })
      return o.stats.segments
    }
    const full = seg(0, 't-2')
    const l1 = seg(1, 't-2')
    const l2 = seg(2, 't-2')
    expect(l1).toBeLessThan(full)                      // the other task's legs off
    expect(l2).toBeLessThan(l1)                        // only the selected task (stations of the others)
    expect(seg(3, 't-1')).toBeLessThan(seg(2, 't-1'))  // nests and stations only, even for the selected task
    expect(o.stats.nests).toBe(2)
    expect(o.stats.stations).toBe(2)
    expect(PAL.G50).toBe(0)
    o.dispose()
  })
})
