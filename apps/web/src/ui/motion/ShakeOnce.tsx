// 12 error-shake (M15-FR-055; transitions.dev 12): one horizontal shake (6 / 4 px, 80 / 60 ms segments, 280 ms total)
// every time `trigger` changes; the caller deduplicates the same alarm within INPUT.shakeDedupMs. Never used on panels,
// the canvas or dialogs (AWR-14 §11.5). reduced and off do not move.
import * as React from 'react'
import { EASE_CSS, MOTION } from '@/lib/tokens/motion.gen'
import { getMotionTier } from './tier'

export function ShakeOnce({ trigger, children }: { trigger: number; children: React.ReactElement<{ ref?: React.Ref<HTMLElement> }> }) {
  const ref = React.useRef<HTMLElement>(null)
  const first = React.useRef(true)
  React.useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    const tier = getMotionTier()
    if (tier === 'reduced' || tier === 'off' || !ref.current) return
    const a = MOTION.shakeDistancePx
    const b = MOTION.shakeOvershootPx
    const total = MOTION.shakeTotalMs
    const segA = MOTION.shakeSegmentAMs / total
    ref.current.animate(
      [
        { transform: 'translateX(0)', offset: 0 },
        { transform: `translateX(${-a}px)`, offset: segA / 2 },
        { transform: `translateX(${a}px)`, offset: segA },
        { transform: `translateX(${-b}px)`, offset: 0.5 + segA / 2 },
        { transform: `translateX(${b}px)`, offset: 0.75 },
        { transform: 'translateX(0)', offset: 1 },
      ],
      { duration: total, easing: EASE_CSS.shake },
    )
  }, [trigger])
  return React.cloneElement(children, { ref })
}
