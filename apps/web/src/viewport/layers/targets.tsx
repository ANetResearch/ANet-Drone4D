// Target layer adapter, TargetsLayer (M18-FR-024, NFR-005; M18-M19 PRD §8.2; AWR-04 §10.5; ADR-110: domain layers belong to
// their module). Owner: M18. Creates the TargetsOverlay (engine/targets: real-size box wireframes <= 32, sensitivity faces and
// outlines <= 64 bodies, 3 draws at most), attaches the targets feed (stores/targets: swarm/target/state 10 Hz,
// target/*/detail 1 Hz into the page model) and rebuilds the overlay in the world phase only when the model or the view
// options changed (<= 10 Hz). The red goes to the target that owns the viewport red (RedArbiter entity kind 'target',
// unacknowledged target.discovered); everything else stays neutral. PerfGovernor (step 1, Tier S first): faces off, then
// bodies of the selected target only, then bodies off. Mounted by WorldCanvas when present (import.meta.glob, the same
// way as layers/environment.tsx; request d2-WP-05-to-M06 also adds 'targets' to LayerId and 'M18' to the owners).
import { useEffect } from 'react'
import { perf, register, type GovernorKnob } from '@/engine'
import { TARGETS_CAPS, TargetsOverlay, targetsModel } from '@/engine/targets'
import { attachTargetsFeed, targetsStore } from '@/stores/targets'
import type { RenderBackend } from '../renderer'
import { registerLayer, type LayerId, type LayerSpec } from './registry'
import { applyLayerVisibility, redOwner } from '../bindings'

export const TARGET_LAYER_ID = 'targets' as unknown as LayerId
const OWNER = 'M18' as unknown as LayerSpec['owner']

/** the mounted overlay (tests and the manual-control HUD read it; null when the layer is not mounted) */
export const targetsLayer: { overlay: TargetsOverlay | null } = { overlay: null }

export function TargetsLayer({ be }: { be: RenderBackend }) {
  useEffect(() => {
    const o = new TargetsOverlay()
    targetsLayer.overlay = o
    const detach = attachTargetsFeed()
    // D2-FX-R2-web (D2-AC-30, "30 targets + sensitivity volumes + 12 vehicles" as the whole scene): the governor may drop
    // the translucent faces (the fill-rate cost on SwiftShader) but never the volume outlines: levels 2 (selected target's
    // bodies only) and 3 (bodies off) of M18-M19 PRD §8.2 hid the sensitivity volumes of the scene the criterion is about
    const levels = [0, 1]
    const knob: GovernorKnob = {
      step: 1, id: 'targets', levels: levels.length, labelKey: 'perf.governor.targets',
      apply: (l) => {
        o.level = levels[l]
      },
      visible: () => o.root.visible && o.nBodies > 0,
    }
    const offs = [
      registerLayer({
        id: TARGET_LAYER_ID, owner: OWNER, perfKey: 'trails', root: o.root, channel: 0,
        caps: { S: { maxInstances: TARGETS_CAPS.targets, maxSegments: TARGETS_CAPS.boxSegs + TARGETS_CAPS.bodySegs },
          BA: { maxInstances: TARGETS_CAPS.targets, maxSegments: TARGETS_CAPS.boxSegs + TARGETS_CAPS.bodySegs } },
        drawCount: () => o.drawCount(),
        warmupVariants: () => [
          { object: o.boxes.obj, before: () => o.boxes.obj.geometry.setDrawRange(0, 2), after: () => o.boxes.commit() },
          { object: o.outlines.obj, before: () => o.outlines.obj.geometry.setDrawRange(0, 2), after: () => o.outlines.commit() },
          {
            object: o.faces,
            before: () => {
              o.faces.geometry.setDrawRange(0, 3)
              o.faces.visible = true
            },
            after: () => {
              const n = o.faces.geometry.drawRange.count
              o.faces.visible = n > 3 && o.nBodies > 0
            },
          },
        ],
        setVisible: (v) => o.setVisible(v),
        knobs: [knob],
        dispose: () => {},
      }),
      perf.registerKnob(knob),
      register('world', 'targets.update', () => {
        const s = targetsStore.getState()
        const red = redOwner()
        o.update(targetsModel, { view: s.bodyView, modelId: s.modelId, selectedId: s.selected, redId: red && red.kind === 'target' ? red.id : null,
          showBodies: true })
      }, { layer: 'trails' }),
    ]
    applyLayerVisibility()
    return () => {
      for (const off of offs) off()
      detach()
      o.dispose()
      if (targetsLayer.overlay === o) targetsLayer.overlay = null
    }
  }, [be])
  return null
}
