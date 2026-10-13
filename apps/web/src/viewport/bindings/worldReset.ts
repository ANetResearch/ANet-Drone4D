// World scope reset of the viewport (D2.2-W1; ADR-111; M06 §7.1). Owner: M06. When the world scope changes (stores/worldScope:
// a world switch, a source switch between the shared show, static browsing and a sandbox, or a sandbox world switch) the
// viewport drops what it holds about the previous world's entities: a sensor view goes back to plain FPV, the camera leaves
// third and FPV (and the follow lock) for orbit without a focus vehicle, the ground pick, the point pick and the GoTo marker
// clear and the mission overlay's draft goes. The drone runtime resets itself (viewport/layers/drones.tsx); targets, field
// and tasking layers redraw from their reset models. One direction only (store -> viewport).
import { events } from '@/engine'
import { onWorldReset } from '@/stores/worldScope'
import { vp } from '../session'
import { sv } from '../sensorView/state'

/** one viewport reset (exported for tests) */
export function resetViewportWorld(): void {
  if (sv.mode !== 'fpv' || sv.sensorNo !== -1) {
    sv.mode = 'fpv'
    sv.sensorNo = -1
    sv.digitalZoom = 1
    sv.changed()
    events.emit('sensorView.mode', { mode: 'fpv' })
  }
  const rig = vp.rig
  if (rig) {
    if (rig.mode === 'third' || rig.mode === 'fpv') rig.setMode('orbit', -1)
    if (rig.followLock) rig.setFollowLock(false)
    rig.focusAgent = -1
  }
  vp.pick = null
  vp.pointPick = null
  vp.mission?.goto.clear()
  vp.mission?.setDraft(null)
  vp.changed()
}

export function installWorldResetBinding(): () => void {
  return onWorldReset('viewport', resetViewportWorld)
}
