// 14 skeleton-reveal (M15-FR-055; transitions.dev 14): while `ready` is false the skeleton pulses once (--pulse-dur), then
// the content fades in over --reveal-dur (blur only in full tier). The skeleton pulse is shadcn Skeleton's own resident
// animation and counts against the loop budget of the page it is on.
import * as React from 'react'
import { EASE_CSS, MOTION } from '@/lib/tokens/motion.gen'
import { getMotionTier } from './tier'

export function SkeletonReveal({ ready, skeleton, children }: { ready: boolean; skeleton: React.ReactNode; children: React.ReactNode }) {
  const ref = React.useRef<HTMLDivElement>(null)
  const was = React.useRef(ready)
  React.useLayoutEffect(() => {
    if (!ready || was.current) {
      was.current = ready
      return
    }
    was.current = true
    const tier = getMotionTier()
    if (tier === 'reduced' || tier === 'off' || !ref.current) return
    ref.current.animate([{ opacity: 0 }, { opacity: 1 }], { duration: MOTION.revealMs, easing: EASE_CSS.inOut })
  }, [ready])
  return <div ref={ref} data-slot="skeleton-reveal">{ready ? children : skeleton}</div>
}
