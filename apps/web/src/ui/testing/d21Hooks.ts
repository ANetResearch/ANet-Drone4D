// D2.1 injection hooks of test builds (M15-FR-010 style; dev and test builds only, constant folded away elsewhere): the
// Playwright specs of perf/m15/d21-*.spec.ts drive what has no backend in the UI-only runs: the link events of the power
// controller (4430 reclaim, 4431 seats full, LIVE), its immediate tick, a perception readout frame and the selected target.
// Added to window.__uxInject next to the existing injectors (events, task draft).
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { power, powerStore } from '@/app/power/PowerController'
import { perceptionFeedIngest, type QuantFrame } from '@/stores/perception'
import { targetsStore } from '@/stores/targets'
import { refreshCapacity } from '@/stores/capacity'
import { targetsModel } from '@/app/shell/engineD2'
import { rtClient } from '@/net/rt'

export function installD21Hooks(): void {
  if (!TEST_SWITCHES || typeof window === 'undefined') return
  const w = window as unknown as { __uxInject?: Record<string, unknown> }
  w.__uxInject = {
    ...w.__uxInject,
    power: {
      conn: (state: string, code: number | null) => power.onConn(state, code),
      tick: () => power.tickNow(),
      state: () => power.state,
      input: () => power.input(),
      view: () => {
        const v = powerStore.getState()
        return { state: v.state, role: v.role, linkOpen: v.linkOpen, frozen: v.frozen, ctx: power.machine.ctx }
      },
    },
    /** select a target and feed one `target/{id}/quant` frame (expanded REST form) through the perception feed */
    quant: (frame: QuantFrame, select = true) => {
      if (select) targetsStore.setState({ selected: frame.target, version: targetsStore.getState().version + 1 })
      perceptionFeedIngest.data({ topic: `target/${frame.target}/quant`, data: frame } as never)
    },
    capacity: () => refreshCapacity(),
    /** where the target list comes from: live rows, store items, roster entries, configurations and details (ids) */
    targets: () => {
      const R = targetsModel.rows
      return {
        rows: Array.from({ length: R.n }, (_x, i) => targetsModel.idOfRow(i)), store: targetsStore.getState().items.map((x) => x.id),
        roster: (rtClient()?.roster.targets?.() ?? []).map((e) => e.id), rosterVersion: rtClient()?.roster.version ?? -1,
        cfgs: [...targetsModel.cfgs.keys()], details: [...targetsModel.details.keys()], modelVersion: targetsModel.version,
      }
    },
  }
}
