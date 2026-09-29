// 03 notification-badge (M15-FR-055, FR-090; transitions.dev 03): the alarm count badge pops in with --ease-bounce when it
// opens and closes faster (--badge-pop-close-dur); count changes are D-class (MotionNumber). The colour (red solid only
// for unacknowledged critical, outline otherwise) is decided by the caller through `variant` (ADR-032 one-red rule).
import * as React from 'react'
import { cn } from '@/lib/utils'
import { EASE_CSS, MOTION } from '@/lib/tokens/motion.gen'
import { fmt } from '@/lib/format'
import { getMotionTier } from './tier'
import { MotionNumber } from './MotionNumber'

export function NotificationDot({ open, count, variant = 'solid', className }: { open: boolean; count?: number; variant?: 'solid' | 'outline'; className?: string }) {
  const ref = React.useRef<HTMLSpanElement>(null)
  const was = React.useRef(open)
  React.useLayoutEffect(() => {
    const el = ref.current
    if (!el || was.current === open) return
    was.current = open
    const tier = getMotionTier()
    if (tier === 'reduced' || tier === 'off') return
    if (open) el.animate([{ opacity: 0, transform: 'scale(0.5)' }, { opacity: 1, transform: 'scale(1)' }], { duration: MOTION.badgePopMs, easing: EASE_CSS.springSnappy })
    else el.animate([{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: 'scale(0.5)' }], { duration: MOTION.badgeCloseMs, easing: EASE_CSS.smoothOut, fill: 'forwards' })
  }, [open])
  return (
    <span
      ref={ref}
      data-slot="notification-dot"
      data-variant={variant}
      data-open={open ? '' : undefined}
      className={cn(
        'inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-hud-cap font-extrabold',
        variant === 'solid' ? 'bg-brand-solid text-brand-foreground' : 'border border-destructive text-brand-text',
        !open && 'invisible',
        className,
      )}
    >
      {count !== undefined && <MotionNumber value={count} format={fmt.count} />}
    </span>
  )
}
