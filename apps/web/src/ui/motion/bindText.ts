// Continuous telemetry text (M15-FR-034; AWR-15 §8.6 class C; ADR-029): one overlay-phase task writes textContent of all
// bound elements at --telemetry-text-interval (Tier S 250 ms, otherwise 100 ms), only when the string changed; no React
// render, no animation. The writes happen on the shared UI tick (stores/uiTick.ts, ADR-066), in the same frame as the
// store summaries, and every bound element is a raster island (BoundText). The job list is mutated only on bind/unbind
// (no allocation per frame).
import { loop, type FrameCtx } from '@/engine'
import { fmt } from '@/lib/format'
import { uiTickDue } from '@/stores/uiTick'

interface TextJob {
  el: HTMLElement
  read: () => number
  format: (v: number) => string
  stale: (() => number) | null
  last: string
}
const jobs: TextJob[] = []
let unregister: (() => void) | null = null
export const bindTextStats = { writes: 0 }

function tick(ctx: FrameCtx): void {
  if (uiTickDue(ctx)) flushTexts()
}

/** write every bound element once (also used when the loop is not running, for example in tests) */
export function flushTexts(): void {
  for (let i = 0; i < jobs.length; i++) {
    const j = jobs[i]
    const age = j.stale ? j.stale() : 0
    const s = age > 0 ? fmt.stale(age) : j.format(j.read())
    if (s !== j.last) {
      j.el.textContent = s
      j.last = s
      bindTextStats.writes++
    }
  }
}

/** bind a C-class value to an element; `stale` returns the data age in seconds (> 0 shows STALE). Returns unbind. */
export function bindText(el: HTMLElement, read: () => number, format: (v: number) => string, stale?: () => number): () => void {
  const job: TextJob = { el, read, format, stale: stale ?? null, last: el.textContent ?? '' }
  jobs.push(job)
  if (!unregister) unregister = loop.register('overlay', 'ui-text', tick, { layer: 'labels' })
  return () => {
    const i = jobs.indexOf(job)
    if (i >= 0) jobs.splice(i, 1)
    if (jobs.length === 0 && unregister) {
      unregister()
      unregister = null
    }
  }
}
