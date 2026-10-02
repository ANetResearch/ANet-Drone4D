// Mission overlay adapter, MissionLayer (M06 §6.10, FR-044, FR-045, AC-034). Owner: M06.
// Creates the MissionOverlay (paths, areas, waypoints, formation slots, targets and the GoTo marker), binds
// stores/mission.ts (M10: paths[vehicle] = {pts, rev}; rows' tracks give the executed index) with event-driven rebuilds
// (<= 4 Hz inside the overlay), pushes its glyphs in the world phase before the glyph commit, and registers the
// 'mission' LayerSpec (thin lines, planned dashed path, area patches) with warm-up variants.
import { useEffect } from 'react'
import { MissionOverlay, register, type MissionData, type MissionPath } from '@/engine'
import { missionStore, type MissionStore } from '@/stores/mission'
import { registerLayer } from './registry'
import { useVp, vp } from '../session'
import { applyLayerVisibility, redOwner } from '../bindings'
import { pointCloudServices } from './registry'

/** mission store -> overlay data (paths only in the D1 store; waypoints and areas when M10 publishes them) */
export function missionDataOf(s: MissionStore): MissionData {
  const paths: MissionPath[] = []
  const doneOf = new Map<string, number>()
  for (const row of s.rows.values()) for (const t of row.tracks) doneOf.set(t.vehicleId, t.item)
  for (const [vehicle, p] of s.paths) paths.push({ vehicle, pts: p.pts, doneIdx: doneOf.get(vehicle) ?? 0, stride: 4 })
  const detail = s.detail as Map<string, { waypoints?: MissionData['waypoints']; areas?: MissionData['areas']; slots?: MissionData['slots']; targets?: MissionData['targets'] }>
  const waypoints: MissionData['waypoints'][number][] = []
  const areas: MissionData['areas'][number][] = []
  const slots: MissionData['slots'][number][] = []
  const targets: MissionData['targets'][number][] = []
  for (const d of detail.values()) {
    if (d?.waypoints) waypoints.push(...d.waypoints)
    if (d?.areas) areas.push(...d.areas)
    if (d?.slots) slots.push(...d.slots)
    if (d?.targets) targets.push(...d.targets)
  }
  return { paths, waypoints, areas, slots, targets }
}

export function MissionLayer() {
  const drones = useVp((s) => s.drones)
  useEffect(() => {
    if (!drones) return
    const m = new MissionOverlay()
    vp.mission = m
    m.setGround((x, y) => vp.groundAt(x, y, pointCloudServices()?.dtm ? (a, b) => pointCloudServices()!.dtm!.sample(a, b) : null))
    m.setData(missionDataOf(missionStore.getState()))
    const offStore = missionStore.subscribe((s) => m.setData(missionDataOf(s)))
    const glyphs = drones.layer.glyphs
    const offs = [
      register('world', 'mission.update', (ctx) => {
        const o = redOwner()
        m.update(ctx, glyphs, o === null || o.kind === 'target')
      }, { order: 100, layer: 'trails' }),
      registerLayer({
        id: 'mission', owner: 'M06', perfKey: 'trails', root: m.root, channel: 0,
        drawCount: () => (m.root.visible ? m.drawCount() : 0),
        warmupVariants: () => [
          { object: m.thin.obj, before: () => m.thin.obj.geometry.setDrawRange(0, 2), after: () => m.thin.commit(m.thin.n) },
          { object: m.planned.obj, before: () => m.planned.warmBefore(), after: () => m.planned.commit() },
          { object: m.patch, before: () => m.patch.geometry.setDrawRange(0, 3), after: () => m.patch.geometry.setDrawRange(0, 0) },
        ],
        setVisible: (v) => {
          m.root.visible = v
        },
        dispose: () => {},
      }),
    ]
    applyLayerVisibility()
    vp.changed()
    return () => {
      offStore()
      for (const off of offs) off()
      m.dispose()
      if (vp.mission === m) vp.mission = null
    }
  }, [drones])
  return null
}
