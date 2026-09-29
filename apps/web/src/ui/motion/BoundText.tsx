// C-class text element (M15-FR-034; AWR-15 §8.6): a span whose textContent is written by bindText (one overlay-phase
// task, Tier S 250 ms, otherwise 100 ms, only when the string changed); React renders it once. `read` and `format` must
// be stable references (module functions or useCallback).
import * as React from 'react'
import { cn } from '@/lib/utils'
import { bindText, flushTexts } from './bindText'

export interface BoundTextProps {
  read: () => number
  format: (v: number) => string
  /** data age in seconds; > 0 shows STALE */
  stale?: () => number
  className?: string
  'aria-label'?: string
}

export function BoundText({ read, format, stale, className, ...rest }: BoundTextProps) {
  const ref = React.useRef<HTMLSpanElement>(null)
  React.useEffect(() => {
    const el = ref.current
    if (!el) return
    el.textContent = format(read())
    const off = bindText(el, read, format, stale)
    flushTexts()
    return off
  }, [read, format, stale])
  return <span ref={ref} data-numeric="" className={cn('tabular-nums', className)} {...rest} />
}
