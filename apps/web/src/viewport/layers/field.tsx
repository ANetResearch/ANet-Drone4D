// Field view layer adapter, FieldLayer (D2.1 WP-11b; AWR-04 §15.11, §15.11.5; ADR-125; UX-FR-159). Owner: M06.
// Creates the FieldRenderer (engine/field: radiation fields of the own vehicles and the targets' perception volumes as
// spherical scatter, one point draw and one shell draw), attaches the perception feed (stores/perception, M19: the store
// subscribes field/{kind}, field/volumes and target/{id}/quant while the view, the volumes or a target selection want them
// and fills engine/perception fieldModel), keeps the store's device class and the point budget on the live form factor, and
// moves the anchors every frame in the world phase (after the drones and the targets). The toolbar, legend and readout card
// are M15's; they drive the view through perceptionActions and the highlight through the viewport facade (field.highlight).
// PerfGovernor knob 'field' (step 1): half the points at most (D2-FX-R2-web, D2-AC-30: the governor never hides a view the
// visitor chose). The renderer keeps the deeper levels of AWR-04 §15.11.5 (selected target's bodies only, no dashed shells,
// off with the 'field.governorOff' notice) for a pinned level (tests) and a future deeper knob.
// Not drawn in the sensor views (sensorClass hidden).
import { useEffect } from 'react'
import { events, perf, register, type GovernorKnob } from '@/engine'
import { FieldRenderer, FIELD_CAPS, FIELD_LEVEL, fieldBudget, type FieldSource } from '@/engine/field'
import { fieldModel } from '@/engine/perception'
import { targetsModel } from '@/engine/targets'
import { rtClient } from '@/net/rt'
import { attachPerceptionFeed, perceptionActions } from '@/stores/perception'
import { selectionStore } from '@/stores/selection'
import type { RenderBackend } from '../renderer'
import { registerLayer, type LayerId, type WarmupItem } from './registry'
import { vp } from '../session'
import { currentFormFactor, onFormFactorChange } from '../quality'

export const FIELD_LAYER_ID: LayerId = 'field'

/** the mounted renderer (facade, tests); null when the layer is not mounted */
export const fieldLayer: { renderer: FieldRenderer | null } = { renderer: null }

/** lookups of the field renderer over the page's roster, drone poses and target rows */
export function fieldSource(): FieldSource {
  const rows = targetsModel.rows
  return {
    model: fieldModel,
    uavs: {
      agentNo: (id) => rtClient()?.roster.agentNoOf(id) ?? -1,
      pose: (a, out) => vp.drones?.poseOf(a, out) ?? false,
    },
    targets: {
      rowOf: (id) => targetsModel.rowOf(id),
      keyOf: (row) => (row < rows.n ? rows.agentNo[row] : -1),
      pos: (row, out) => {
        out[0] = rows.pos[3 * row]
        out[1] = rows.pos[3 * row + 1]
        out[2] = rows.pos[3 * row + 2]
      },
      yaw: (row) => rows.heading[row],
      body: (id, row, idx) => targetsModel.bodiesOf(id, targetsModel.classOfRow(row))[idx] ?? null,
    },
    volumeModel: () => {
      const rt = rtClient()
      const p = selectionStore.getState().primary
      const a = p !== null && rt ? rt.roster.agentNoOf(p) : -1
      return a >= 0 ? rt?.roster.get(a)?.model ?? null : null
    },
    structureVersion: () => rtClient()?.roster.version ?? -1,
  }
}

export function FieldLayer({ be }: { be: RenderBackend }) {
  useEffect(() => {
    const r = new FieldRenderer({ pointSizeMode: be.pointSizeMode, createPointsMaterial: () => be.createPointsMaterial(), budget: fieldBudget(currentFormFactor(), be.tier) })
    fieldLayer.renderer = r
    const applyFf = (ff = currentFormFactor()): void => {
      // a mobile GPU on a desktop-sized screen keeps the tablet budget (viewport/quality.ts)
      const eff = ff === 'desktop' && be.quality.mobile ? 'tablet' : ff
      r.setBudget(fieldBudget(eff, be.tier), `${eff}-${be.tier}`)
      perceptionActions.setDeviceClass(ff)
    }
    applyFf()
    const detach = attachPerceptionFeed()
    const src = fieldSource()
    const knob: GovernorKnob = {
      // D2-FX-R2-web (D2-AC-30, D2-AC-53): half the points at most; the governor never hides a field view the visitor chose
      // (the selected-only, no-dashes and off levels left the whole-scene measurement without its D2 layers)
      step: 1, id: 'field', levels: FIELD_LEVEL.halfPoints + 1, labelKey: 'perf.governor.field',
      apply: (l) => {
        r.setGovernorLevel(l)
        if (l >= FIELD_LEVEL.off && r.pinnedLevel === null) events.emit('field.governorOff', { off: true })
      },
      visible: () => r.root.visible && r.stats.points + r.stats.circles > 0,
    }
    const warm = (): WarmupItem[] => r.warmItems()
    const offs = [
      registerLayer({
        id: FIELD_LAYER_ID, owner: 'M06', perfKey: 'trails', root: r.root, channel: 0, sensorClass: 'hidden',
        caps: { S: { maxInstances: FIELD_CAPS.points, maxSegments: FIELD_CAPS.circles * 48 }, BA: { maxInstances: FIELD_CAPS.points, maxSegments: FIELD_CAPS.circles * 48 } },
        drawCount: () => r.drawCount(),
        warmupVariants: warm,
        setVisible: (v) => r.setVisible(v),
        knobs: [knob],
        dispose: () => {},
      }),
      perf.registerKnob(knob),
      register('world', 'field.update', (ctx) => r.update(ctx, src, performance.now()), { order: 40, layer: 'trails' }),
      onFormFactorChange(applyFf),
    ]
    return () => {
      for (const off of offs) off()
      detach()
      r.dispose()
      if (fieldLayer.renderer === r) fieldLayer.renderer = null
    }
  }, [be])
  return null
}
