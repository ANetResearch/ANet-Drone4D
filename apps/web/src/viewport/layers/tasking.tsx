// Tasking layer adapter, TaskingLayer (M20 PRD §8 item 2; M20-FR-006, FR-022, FR-023, FR-052; AWR-04 §10.5; ADR-110: domain
// layers belong to their module). Owner: M20. Draws the line lists of stores/tasking.ts `buildTaskingLines` (nests,
// sub-areas, strips, legs, fills, stations, the dry-run preview with its keepout band; neutral palette with per-vehicle
// styles, the selected task as a 2 CSS px g50 line that stays visible and easy to tap on phones and tablets at DPR 2-3).
// Two draws at most: one ThinLineBatch (1 px native lines, 4096 segments) and one WideLineBatch (selected task, 512
// segments), the line programs of engine/mission/lineBatch (M06): two vertex attributes, a handful of vec3 uniforms, no
// textures, no instancing; within the WebGL2 minimum limits (ADR-086/087; tests/m20/overlay.browser.test.ts emulates
// 24 / 12 / 12 / 24 and 16 384 B like perf/m06/gpu-limits.spec.ts). Rebuilds are event driven (store version, <= 4 Hz)
// and allocate nothing once the buffers fit. PerfGovernor (step 1): 0 full, 1 other tasks' legs off, 2 only the selected
// task, nests and stations, 3 nests and stations only. In the sensor views the layer is an EO overlay (registry default).
import { useEffect } from 'react'
import { Group } from 'three'
import { perf, register, type FrameCtx, type GovernorKnob } from '@/engine'
import { ThinLineBatch, WideLineBatch } from '@/engine/mission'
import { LINE_CAPS, attachTaskingFeed, buildTaskingLines, taskingStore, type LineStats, type TaskingState } from '@/stores/tasking'
import type { RenderBackend } from '../renderer'
import { registerLayer, pointCloudServices, type LayerId, type LayerSpec } from './registry'
import { applyLayerVisibility } from '../bindings'
import { vp } from '../session'

export const TASKING_LAYER_ID: LayerId = 'tasking'
const OWNER: LayerSpec['owner'] = 'M20'

export interface OverlayInput {
  state: TaskingState
  /** ground height (m) for 2D geometry and nests without z */
  ground: (x: number, y: number) => number
}

export class TaskingOverlay {
  readonly root = new Group()
  readonly thin = new ThinLineBatch(LINE_CAPS.thin, 'TaskingLines', 24)
  readonly wide = new WideLineBatch(LINE_CAPS.wide, 2, false, 'TaskingSelected', 25, 0.9)
  /** PerfGovernor level (see the header) */
  level = 0
  /** counts of the last build (tests, HUD) */
  stats: LineStats = { segments: 0, wide: 0, stations: 0, nests: 0, fills: 0, strips: 0, keepouts: 0, tasks: 0, clipped: false }
  private seen = ''

  constructor() {
    this.root.add(this.thin.obj, this.wide.obj)
    this.root.name = 'TaskingLayer'
  }

  drawCount(): number {
    return this.root.visible ? this.thin.drawCount() + this.wide.drawCount() : 0
  }

  setVisible(v: boolean): void {
    this.root.visible = v
  }

  /** per-frame view uniforms of the wide line (drawing buffer, DPR, camera near plane) */
  frame(ctx: FrameCtx): void {
    if (!this.wide.obj.visible) return
    this.wide.setWidth(ctx.dpr > 0 ? ctx.dpr : 1)
    this.wide.setView(ctx.dbW, ctx.dbH, (ctx.camera as { near?: number } | null)?.near ?? 0)
  }

  /** rebuild when the store version or the governor level changed; returns true when it rebuilt */
  update(inp: OverlayInput): boolean {
    const key = `${inp.state.version}|${this.level}`
    if (key === this.seen) return false
    this.seen = key
    this.thin.begin()
    this.wide.begin()
    this.stats = buildTaskingLines(inp.state, inp.ground, this.level, this.thin, this.wide)
    this.thin.commit()
    this.wide.commit()
    return true
  }

  dispose(): void {
    this.thin.dispose()
    this.wide.dispose()
  }
}

/** the mounted overlay (tests and the HUD read it; null when the layer is not mounted) */
export const taskingLayer: { overlay: TaskingOverlay | null } = { overlay: null }

function groundOf(x: number, y: number): number {
  const dtm = pointCloudServices()?.dtm
  return vp.groundAt(x, y, dtm ? (a, b) => dtm.sample(a, b) : null)
}

export function TaskingLayer({ be }: { be: RenderBackend }) {
  useEffect(() => {
    const o = new TaskingOverlay()
    taskingLayer.overlay = o
    const detach = attachTaskingFeed()
    const knob: GovernorKnob = {
      // D2-FX-R2-web (D2-AC-30): the other tasks' legs at most (nests, stations, strips and the selected task stay on screen)
      step: 1, id: 'tasking', levels: 2, labelKey: 'perf.governor.tasking',
      apply: (l) => {
        o.level = l
      },
      visible: () => o.root.visible && o.stats.segments > 0,
    }
    const caps = { maxSegments: LINE_CAPS.thin + LINE_CAPS.wide }
    const offs = [
      registerLayer({
        id: TASKING_LAYER_ID, owner: OWNER, perfKey: 'trails', root: o.root, channel: 0, caps: { S: caps, BA: caps },
        drawCount: () => o.drawCount(),
        warmupVariants: () => [
          { object: o.thin.obj, before: () => o.thin.obj.geometry.setDrawRange(0, 2), after: () => o.thin.commit() },
          { object: o.wide.obj, before: () => o.wide.warmBefore(), after: () => o.wide.commit() },
        ],
        setVisible: (v) => o.setVisible(v),
        knobs: [knob],
        dispose: () => {},
      }),
      perf.registerKnob(knob),
      register('world', 'tasking.update', (ctx) => {
        o.update({ state: taskingStore.getState(), ground: groundOf })
        o.frame(ctx)
      }, { layer: 'trails' }),
    ]
    applyLayerVisibility()
    return () => {
      for (const off of offs) off()
      detach()
      o.dispose()
      if (taskingLayer.overlay === o) taskingLayer.overlay = null
    }
  }, [be])
  return null
}
