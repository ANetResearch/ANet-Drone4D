// stores/layers visibility -> registered viewport layers (M06 §7.1 layers.setVisible; AWR-14 §6.15) and the label
// preference of stores/prefs (ui.labels). Owner: M06. The point cloud (M05) and environment (M07) bind themselves.
import { layersStore, type LayerId as StoreLayerId } from '@/stores/layers'
import { prefsStore } from '@/stores/prefs'
import { getLayer, onLayersChanged, type LayerId } from '../layers/registry'
import { pcOnlyScene } from '../bench'

const MAP: readonly (readonly [StoreLayerId, LayerId])[] = [
  ['drones', 'drones'], ['trails', 'trails'], ['frustums', 'frustums'], ['mission', 'mission'], ['zones', 'zones'], ['labels', 'labels'],
]

export function applyLayerVisibility(): void {
  const v = layersStore.getState().visible
  const labelsPref = prefsStore.getState().ui.labels
  // flight60 scene=pc: the point cloud alone, every M06 layer off (AWR-18 §8.6(5))
  const pcOnly = pcOnlyScene()
  for (const [sk, lk] of MAP) {
    const spec = getLayer(lk)
    if (!spec) continue
    spec.setVisible(!pcOnly && (lk === 'labels' ? v.labels && labelsPref : v[sk]))
  }
  if (pcOnly) for (const id of PC_ONLY_OFF) getLayer(id)?.setVisible(false)
}
const PC_ONLY_OFF: readonly LayerId[] = ['groundSky', 'glyphs', 'debug']

export function installLayersBinding(): () => void {
  applyLayerVisibility()
  const a = layersStore.subscribe(applyLayerVisibility)
  const b = prefsStore.subscribe((s, prev) => {
    if (s.ui.labels !== prev.ui.labels) applyLayerVisibility()
  })
  const c = onLayersChanged(applyLayerVisibility)
  return () => {
    a()
    b()
    c()
  }
}
