// C-class text element (M15-FR-034; AWR-15 §8.6): a span whose textContent is written by bindText (one overlay-phase
// task, Tier S 250 ms, otherwise 100 ms, only when the string changed); React renders it once. `read` and `format` must
// be stable references (module functions or useCallback). The span is a raster island (ADR-066: its own small compositor
// layer, so the 4 Hz write never re-rasters the strip or panel around it); `island={false}` when an enclosing element is
// already the island (one layer per DroneRail row instead of one per value).
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
  /** own raster island (default); false inside an element that already carries data-island */
  island?: boolean
}

export function BoundText({ read, format, stale, className, island = true, ...rest }: BoundTextProps) {
  const ref = React.useRef<HTMLSpanElement>(null)
  React.useEffect(() => {
    const el = ref.current
    if (!el) return
    el.textContent = format(read())
    const off = bindText(el, read, format, stale)
    flushTexts()
    return off
  }, [read, format, stale])
  return <span ref={ref} data-numeric="" data-island={island ? '' : undefined} className={cn('tabular-nums', className)} {...rest} />
}
