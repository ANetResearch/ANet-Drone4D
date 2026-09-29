// Trail layer adapter (M06 §6.10, FR-042, FR-043, AC-033). Owner: M06.
// Registers the 'trails' LayerSpec over the drone runtime's three trail batches (focus set 1 px, selected 2 px, halo
// 4 px; 3 draws) with their warm-up variants and PerfGovernor step 1: Tier S 16 x 256 -> 8 x 128 -> 4 x 64 -> selected
// only; Tier B/A 64 x 1024 -> 32 x 512 -> 16 x 256 -> selected only.
import { useEffect } from 'react'
import { perf, type GovernorKnob, type TrailBatch } from '@/engine'
import { registerLayer, type WarmupItem } from './registry'
import { useVp, vp } from '../session'
import { applyLayerVisibility } from '../bindings'

export const TRAIL_LEVELS = {
  S: [[16, 256], [8, 128], [4, 64], [0, 64]],
  BA: [[64, 1024], [32, 512], [16, 256], [0, 256]],
} as const

export function TrailsLayer() {
  const drones = useVp((s) => s.drones)
  useEffect(() => {
    if (!drones) return
    const L = drones.layer
    const S = vp.be?.tier === 'S'
    const levels = S ? TRAIL_LEVELS.S : TRAIL_LEVELS.BA
    const knob: GovernorKnob = { step: 1, id: 'trails', levels: levels.length, labelKey: 'perf.governor.trails', apply: (l) => L.setTrailLimits(levels[l][0], levels[l][1]) }
    const batches: TrailBatch[] = [L.trailHalo, L.trailSel, L.trailFocus]
    const warm = (): WarmupItem[] => batches.map((b) => {
      const g = b.mesh.geometry as unknown as { instanceCount: number }
      return { object: b.mesh, before: () => (g.instanceCount = 1), after: () => (g.instanceCount = b.maxSlots * b.maxSegs) }
    })
    const offs = [
      registerLayer({
        id: 'trails', owner: 'M06', perfKey: 'trails', root: L.trailRoot, channel: 0,
        caps: { S: { maxSegments: 16 * 256 }, BA: { maxSegments: 64 * 1024 } },
        drawCount: () => (L.trailRoot.visible ? L.drawCountTrails() : 0),
        warmupVariants: warm,
        setVisible: (v) => {
          L.trailRoot.visible = v
        },
        knobs: [knob],
        dispose: () => {},
      }),
      perf.registerKnob(knob),
    ]
    applyLayerVisibility()
    return () => {
      for (const off of offs) off()
    }
  }, [drones])
  return null
}
