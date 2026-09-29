// store -> viewport glue (M06 §6.1 bindings/*): selection highlights and 60 Hz channels, the viewport RedArbiter,
// layer visibility and the label preference. One direction only (stores -> facade/engine). Owner: M06.
import { installLayersBinding } from './layersVisibility'
import { installRedBinding } from './redOwner'
import { installSelectionBinding } from './selection'

export function installBindings(): () => void {
  const offs = [installSelectionBinding(), installRedBinding(), installLayersBinding()]
  return () => {
    for (const off of offs) off()
  }
}
export { agentNoOf } from './selection'
export { redOwner, setRedOverride, evaluateRed } from './redOwner'
export { applyLayerVisibility } from './layersVisibility'
