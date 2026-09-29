// Morph concurrency budget K = 8 (M15-FR-063; d03 §0 item 4; ADR-030): a morph occupies a slot for the settle time of its
// spring; when all slots are taken the change is applied with set (no animation) and counted as denied. Only mounted
// visible rows and selected rows switch at all (list virtualisation guarantees it). Counters live in __perf.ui.icons.
import { MOTION_BUDGET } from '@/lib/tokens/input.gen'
import { PERF_UI } from '@/ui/shell/perfUi'

let active = 0
export const morphBudget = {
  get active(): number {
    return active
  },
  acquire(settleMs: number): boolean {
    if (active >= MOTION_BUDGET.morphK) {
      PERF_UI.icons.denied++
      return false
    }
    active++
    PERF_UI.icons.active = active
    if (active > PERF_UI.icons.activeMax) PERF_UI.icons.activeMax = active
    setTimeout(() => {
      active--
      PERF_UI.icons.active = active
    }, settleMs)
    return true
  },
  reset(): void {
    active = 0
    PERF_UI.icons.active = 0
  },
}
