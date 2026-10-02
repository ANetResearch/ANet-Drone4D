// The single LfScheduler of the application (M15-FR-072; ADR-031; d01 §3.6.2; AWR-10 §6.7 item 3). It runs as the
// overlay-phase task 'lf-scheduler' of the engine loop and falls back to its own requestAnimationFrame only while the loop
// is not running (report pages, the small-window Empty state). Per frame at most 2 charts are redrawn and at most 2 ms are
// spent; a chart is redrawn only when its data version changed and its rate allows it (HUD 4 Hz, focused chart <= 10 Hz,
// Tier S <= 4 Hz); invisible charts (IntersectionObserver, document.hidden, hidden tab or collapsed rail via setVisible)
// are never drawn. Tier S shows at most 4 streaming charts; slots go to focused > HUD > detail > others. On Tier S the
// redraws happen only in frames of the shared UI tick (stores/uiTick.ts, ADR-066), together with the store summaries and
// the C-class text, so that the frames in between carry no canvas upload; the rate check tolerates the 1 ms frame
// quantisation of the tick.
import { ctx as frameCtx, loop, type FrameCtx, type Tier } from '@/engine'
import { LF } from '@/lib/tokens/input.gen'
import { PERF_UI } from '@/ui/shell/perfUi'
import { uiTickDue } from '@/stores/uiTick'

export type LfPrio = 0 | 1 | 2 | 3
export interface LfJobSpec {
  el: Element
  streaming: boolean
  hz: number
  prio: LfPrio
  version(): number
  draw(nowMs: number): void
  onSlot?(has: boolean): void
}
interface LfJob extends LfJobSpec {
  ioVisible: boolean
  panelVisible: boolean
  hasSlot: boolean
  last: number
  seen: number
  basePrio: LfPrio
}

const jobs: LfJob[] = []
let rr = 0
let slotsDirty = true
let focused: Element | null = null
let unregister: (() => void) | null = null
let fallbackRaf = 0
const io = typeof IntersectionObserver === 'function'
  ? new IntersectionObserver((entries) => {
      for (const e of entries) {
        const j = jobs.find((x) => x.el === e.target)
        if (j && j.ioVisible !== e.isIntersecting) {
          j.ioVisible = e.isIntersecting
          slotsDirty = true
        }
      }
    })
  : null

const visible = (j: LfJob) => j.ioVisible && j.panelVisible

function assignSlots(tier: Tier): void {
  const cap = tier === 'S' ? LF.streamingSlotsTierS : Number.POSITIVE_INFINITY
  const cand = jobs.filter((j) => j.streaming && visible(j)).sort((a, b) => b.prio - a.prio || a.last - b.last)
  for (let i = 0; i < jobs.length; i++) {
    const j = jobs[i]
    const has = !j.streaming || (visible(j) && cand.indexOf(j) < cap)
    if (has !== j.hasSlot) {
      j.hasSlot = has
      j.onSlot?.(has)
    }
  }
  PERF_UI.charts.streamingVisible = Math.min(cand.length, cap)
  slotsDirty = false
}

export function schedulerFrame(nowMs: number, tier: Tier): number {
  if (typeof document !== 'undefined' && document.hidden) return 0
  if (slotsDirty) assignSlots(tier)
  const t0 = performance.now()
  let drawn = 0
  const n = jobs.length
  for (let c = 0; c < n && drawn < LF.drawsPerFrame && performance.now() - t0 < LF.drawBudgetMs; c++) {
    const j = jobs[(rr + c) % n]
    if (!visible(j) || (j.streaming && !j.hasSlot)) continue
    const hz = tier === 'S' ? Math.min(j.hz, LF.focusHzTierS) : j.hz
    if (nowMs - j.last < 1000 / hz - 1) continue
    const v = j.version()
    if (v === j.seen) continue
    if (!j.streaming && j.last > 0) {
      const svgHz = 1000 / Math.max(1, nowMs - j.last)
      if (svgHz > PERF_UI.charts.svgHzMax) PERF_UI.charts.svgHzMax = svgHz
    }
    j.seen = v
    j.last = nowMs
    j.draw(nowMs)
    drawn++
  }
  rr = (rr + 1) % Math.max(1, n)
  if (drawn > PERF_UI.charts.drawsMax) PERF_UI.charts.drawsMax = drawn
  const ms = performance.now() - t0
  if (ms > PERF_UI.charts.drawMsMax) PERF_UI.charts.drawMsMax = ms
  return drawn
}

function loopTask(c: FrameCtx): void {
  if (c.tier === 'S' && !uiTickDue(c)) return
  schedulerFrame(c.nowMs, c.tier)
}
function fallback(now: number): void {
  fallbackRaf = 0
  if (loop.running || jobs.length === 0) return
  schedulerFrame(now, frameCtx.tier)
  fallbackRaf = requestAnimationFrame(fallback)
}
function kick(): void {
  if (!unregister) {
    unregister = loop.register('overlay', 'lf-scheduler', loopTask, { layer: 'hudCharts' })
    loop.onRunningChange((running) => {
      if (!running) kick()
    })
  }
  if (!loop.running && !fallbackRaf && jobs.length && typeof requestAnimationFrame === 'function') fallbackRaf = requestAnimationFrame(fallback)
}

export const lfScheduler = {
  add(spec: LfJobSpec): () => void {
    const j: LfJob = { ...spec, ioVisible: io === null, panelVisible: true, hasSlot: !spec.streaming, last: 0, seen: -1, basePrio: spec.prio }
    jobs.push(j)
    io?.observe(spec.el)
    slotsDirty = true
    kick()
    return () => {
      const i = jobs.indexOf(j)
      if (i >= 0) jobs.splice(i, 1)
      io?.unobserve(spec.el)
      slotsDirty = true
    }
  },
  /** panel-level visibility (hidden Dock tab, collapsed rail): hidden charts are never drawn */
  setVisible(el: Element, v: boolean): void {
    for (const j of jobs) {
      if (j.el === el || el.contains(j.el)) {
        if (j.panelVisible !== v) {
          j.panelVisible = v
          slotsDirty = true
        }
      }
    }
  },
  /** the focused chart gets the highest slot priority */
  focus(el: Element | null): void {
    focused = el
    for (const j of jobs) j.prio = j.el === focused ? 3 : j.basePrio
    slotsDirty = true
  },
  /** force the next frame to redraw this chart (theme change, resize) */
  invalidate(el: Element): void {
    for (const j of jobs) if (j.el === el) j.seen = -1
  },
  get size(): number {
    return jobs.length
  },
}
