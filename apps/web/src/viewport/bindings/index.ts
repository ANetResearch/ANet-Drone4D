// store -> viewport glue (M06 §6.1 bindings/*): selection highlights and 60 Hz channels, the viewport RedArbiter,
// layer visibility and the label preference; D2: the targets as thermal emitters of the LWIR view. One direction only
// (stores -> facade/engine). Owner: M06.
// D2.2-W1: the world scope reset (camera follow, sensor view, picks and the mission overlay's draft; bindings/worldReset.ts).
import { installLayersBinding } from './layersVisibility'
import { installRedBinding } from './redOwner'
import { installSelectionBinding } from './selection'
import { installTargetThermal } from './targetThermal'
import { installWorldResetBinding } from './worldReset'

export function installBindings(): () => void {
  const offs = [installSelectionBinding(), installRedBinding(), installLayersBinding(), installTargetThermal(), installWorldResetBinding()]
  return () => {
    for (const off of offs) off()
  }
}
export { agentNoOf } from './selection'
export { redOwner, setRedOverride, evaluateRed } from './redOwner'
export { applyLayerVisibility } from './layersVisibility'
