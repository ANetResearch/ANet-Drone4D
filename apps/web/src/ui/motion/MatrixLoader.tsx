// 31 matrix-loader (M15-FR-055; transitions.dev 31): a 3 x 3 dot loader for streaming indicators and in-button progress;
// a resident loop under the loop budget (static dots when no slot).
import * as React from 'react'
import { cn } from '@/lib/utils'
import { useLoopSlot } from './useLoopSlot'

const DOTS = [0, 1, 2, 3, 4, 5, 6, 7, 8]
export function MatrixLoader({ variant = 'scan', priority = 0, className, label }: { variant?: 'scan' | 'pulse'; priority?: number; className?: string; label?: string }) {
  const ref = React.useRef<HTMLSpanElement>(null)
  const on = useLoopSlot(ref, priority)
  return (
    <span ref={ref} className={cn('t-matrix', className)} data-loop={on ? 'on' : 'off'} data-variant={variant} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      {DOTS.map((i) => (
        <i key={i} style={{ '--i': variant === 'scan' ? i % 3 : i } as React.CSSProperties} />
      ))}
    </span>
  )
}
