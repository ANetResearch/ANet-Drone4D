// UI animation sampler of PerfGovernor step 6 (M06-FR-076; ADR-067, ADR-071 item 5, ADR-076). Owner: M06.
// The motion knob is "visible" only while a UI animation that the motion tier changes is running. The answer is sampled
// about once per second in an idle callback (style is clean after the frame's rendering update, so
// document.getAnimations() does not force a recalculation inside the loop callback, ADR-071 item 5) and only animations on
// the document timeline count: scroll-driven ones never change with html[data-motion] (ADR-076).
import { onPowerFrozen, powerFrozen } from '@/lib/powerFreeze'

/**
 * whether an animation is one the motion tier changes: running on the document timeline (time-driven CSS animations,
 * transitions and WAAPI). Scroll-driven animations (ScrollTimeline, ViewTimeline: the edge fades of .fade-scroll-y) are
 * progress-based, report playState 'running' for as long as their element exists and are not touched by
 * html[data-motion]; counting them made PerfGovernor step 6 always "visible", so every Tier S session raised a motion
 * Toast (and its first compositor raster) about 5 s after the reveal although nothing on screen changed (FX2-R5, ADR-076)
 */
export function motionAffected(a: Animation, doc: Document = document): boolean {
  if (a.playState !== 'running') return false
  const tl = a.timeline
  if (!tl) return false
  if (tl === doc.timeline) return true
  const DT = (globalThis as { DocumentTimeline?: typeof DocumentTimeline }).DocumentTimeline
  return typeof DT === 'function' && tl instanceof DT
}

/**
 * whether some UI animation or transition that the motion tier affects runs (motionAffected), sampled about once per
 * second in an idle callback (after the frame's rendering update, when style is clean, so getAnimations() does not force
 * a recalculation inside the loop callback)
 */
export function installAnimationSampler(periodMs = 1000): { readonly animating: boolean; dispose(): void } {
  const st = { animating: true, dispose: (): void => {} }
  if (typeof document === 'undefined' || typeof document.getAnimations !== 'function') return st
  let timer: ReturnType<typeof setTimeout> | null = null
  let idle = 0
  const ric = (globalThis as { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number }).requestIdleCallback
  const cic = (globalThis as { cancelIdleCallback?: (h: number) => void }).cancelIdleCallback
  const sample = (): void => {
    idle = 0
    st.animating = document.getAnimations().some((x) => motionAffected(x))
    // power-save mode (D2-INT, d2-WP-12c-to-M06 item 5; D2-AC-44, D2-AC-46): the loop is suspended and PerfGovernor does not
    // step, so the sampler stops until the page resumes (then it samples at once)
    timer = powerFrozen() ? null : setTimeout(schedule, periodMs)
  }
  const schedule = (): void => {
    timer = null
    if (ric) idle = ric(sample, { timeout: periodMs })
    else timer = setTimeout(sample, 0)
  }
  const cancel = (): void => {
    if (timer) clearTimeout(timer)
    if (idle && cic) cic(idle)
    timer = null
    idle = 0
  }
  const offFreeze = onPowerFrozen((on) => {
    if (on) cancel()
    else if (!timer && !idle) schedule()
  })
  if (!powerFrozen()) schedule()
  st.dispose = () => {
    offFreeze()
    cancel()
  }
  return st
}
