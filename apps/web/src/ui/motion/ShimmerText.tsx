// 15 shimmer-text (M15-FR-055; transitions.dev 15): the text shimmer is a resident loop, so it runs only while the loop
// budget grants a slot (<= 2 on screen, priority based, paused off screen via IntersectionObserver); otherwise the text is
// static. reduced and off are static (tiers.css).
import * as React from 'react'
import { cn } from '@/lib/utils'
import { useLoopSlot } from './useLoopSlot'

export function ShimmerText({ children, priority = 0, className }: { children: string; priority?: number; className?: string }) {
  const ref = React.useRef<HTMLSpanElement>(null)
  const on = useLoopSlot(ref, priority)
  return (
    <span ref={ref} className={cn('t-shimmer', className)} data-loop={on ? 'on' : 'off'}>
      {children}
    </span>
  )
}
