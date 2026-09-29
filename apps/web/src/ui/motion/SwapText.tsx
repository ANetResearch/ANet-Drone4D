// 04 text-states-swap for S-class state text (M15-FR-055; transitions.dev 04): the old text leaves upward and the new
// text enters from below (--text-swap-translate-y, --text-swap-dur, ease-in-out), blur only in full tier with a blur
// slot; reduced and off swap instantly. Two stacked spans in a single grid cell keep the layout stable.
import * as React from 'react'
import { cn } from '@/lib/utils'
import { EASE_CSS, MOTION } from '@/lib/tokens/motion.gen'
import { getMotionTier, useMotionTier } from './tier'
import { motionBudget } from './budget'

export function SwapText({ value, className }: { value: string; className?: string }) {
  const tier = useMotionTier()
  const [shown, setShown] = React.useState(value)
  const [leaving, setLeaving] = React.useState<string | null>(null)
  const inRef = React.useRef<HTMLSpanElement>(null)
  const outRef = React.useRef<HTMLSpanElement>(null)

  // follow `value` while rendering: the old text becomes the leaving span unless the tier swaps instantly
  if (value !== shown) {
    setLeaving(tier === 'reduced' || tier === 'off' ? null : shown)
    setShown(value)
  }

  React.useLayoutEffect(() => {
    if (leaving === null) return
    const blur = getMotionTier() === 'full' && motionBudget.acquireBlur(MOTION.textSwapMs)
    const d = MOTION.textSwapDistancePx
    const timing: KeyframeAnimationOptions = { duration: MOTION.textSwapMs, easing: EASE_CSS.inOut, fill: 'both' }
    const hidden = (y: number): Keyframe => (blur ? { opacity: 0, transform: `translateY(${y}px)`, filter: `blur(${MOTION.blurSmallPx}px)` } : { opacity: 0, transform: `translateY(${y}px)` })
    const visible: Keyframe = blur ? { opacity: 1, transform: 'translateY(0)', filter: 'blur(0px)' } : { opacity: 1, transform: 'translateY(0)' }
    inRef.current?.animate([hidden(d), visible], timing)
    const a = outRef.current?.animate([visible, hidden(-d)], timing)
    a?.finished.then(() => setLeaving(null), () => setLeaving(null))
  }, [leaving])

  return (
    <span className={cn('t-swap', className)} aria-live="off">
      {leaving !== null && <span ref={outRef} aria-hidden="true">{leaving}</span>}
      <span ref={inRef}>{shown}</span>
    </span>
  )
}
