// 02 number-pop-in for D-class values (M15-FR-055; AWR-15 §8.6; d02 §4.4): only the digits that changed are replayed
// (WAAPI: slide from --digit-distance, fade, blur when a blur slot is granted in full tier), at most one animation per
// field per --telemetry-anim-interval (values arriving meanwhile are held and applied at the end of the interval), and
// the site wide pop-in bucket (24 per second) decides whether a change animates at all. reduced and off assign directly.
// The span is a raster island (ADR-066) unless `island={false}` (an enclosing element already is one).
import * as React from 'react'
import { cn } from '@/lib/utils'
import { EASE_CSS, MOTION } from '@/lib/tokens/motion.gen'
import { getMotionTier } from './tier'
import { motionBudget } from './budget'

export interface MotionNumberProps {
  value: number
  format: (v: number) => string
  minIntervalMs?: number
  className?: string
  'aria-label'?: string
  /** own raster island (default); false inside an element that already carries data-island */
  island?: boolean
}

/** positions (from the right) whose characters differ; strings are right aligned like tabular numbers */
export function changedDigits(prev: string, next: string): boolean[] {
  const out: boolean[] = new Array(next.length)
  for (let i = 0; i < next.length; i++) {
    const p = prev.length - (next.length - i)
    out[i] = p < 0 || prev[p] !== next[i]
  }
  return out
}

/**
 * one .t-digit span per character, reused in place: only the spans whose character changed get a new text (and an
 * animation), so a value change is a few text writes instead of rebuilding the subtree (ADR-066: less style and layout
 * work in the UI tick frame)
 */
function render(el: HTMLElement, text: string, prev: string, animate: boolean, blur: boolean): number {
  const changed = changedDigits(prev, text)
  while (el.childNodes.length > text.length) el.removeChild(el.firstChild!)
  while (el.childNodes.length < text.length) {
    const span = document.createElement('span')
    span.className = 't-digit'
    el.insertBefore(span, el.firstChild)
  }
  let animated = 0
  for (let i = 0; i < text.length; i++) {
    const span = el.childNodes[i] as HTMLElement
    if (span.textContent !== text[i]) span.textContent = text[i]
    if (animate && changed[i] && /[0-9]/.test(text[i])) {
      const from: Keyframe = blur
        ? { opacity: 0, transform: `translateY(${MOTION.digitDistancePx}px)`, filter: `blur(${MOTION.blurSmallPx}px)` }
        : { opacity: 0, transform: `translateY(${MOTION.digitDistancePx}px)` }
      const to: Keyframe = blur ? { opacity: 1, transform: 'translateY(0)', filter: 'blur(0px)' } : { opacity: 1, transform: 'translateY(0)' }
      span.animate([from, to], { duration: MOTION.digitMs, easing: EASE_CSS.digit, delay: animated * MOTION.digitStaggerMs, fill: 'backwards' })
      animated++
    }
  }
  el.dataset.value = text
  return animated
}

export function MotionNumber({ value, format, minIntervalMs = MOTION.telemetryAnimIntervalMs, className, island = true, ...rest }: MotionNumberProps) {
  const ref = React.useRef<HTMLSpanElement>(null)
  const st = React.useRef({ shown: '', lastAnimMs: Number.NEGATIVE_INFINITY, timer: 0 as ReturnType<typeof setTimeout> | 0, latest: '' })

  React.useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const s = st.current
    const text = format(value)
    s.latest = text
    if (text === s.shown) return
    const apply = () => {
      s.timer = 0
      const next = s.latest
      if (next === s.shown) return
      const now = performance.now()
      const tier = getMotionTier()
      const animate = s.shown !== '' && tier !== 'reduced' && tier !== 'off' && motionBudget.acquirePop(now)
      render(el, next, s.shown, animate, animate && tier === 'full' && motionBudget.acquireBlur(MOTION.digitMs))
      if (animate) s.lastAnimMs = now
      s.shown = next
    }
    const wait = s.lastAnimMs + minIntervalMs - performance.now()
    if (s.shown === '' || wait <= 0) apply()
    else if (!s.timer) s.timer = setTimeout(apply, wait)
  }, [value, format, minIntervalMs])

  React.useEffect(() => () => {
    if (st.current.timer) clearTimeout(st.current.timer)
  }, [])

  return <span ref={ref} data-numeric="" data-island={island ? '' : undefined} className={cn('t-number', className)} {...rest} />
}
