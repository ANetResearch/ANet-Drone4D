// KPI row (M15-FR-070; R09 .kpi, G18; g07 §5.1): label (text-hud-cap, upper case), value (text-hud-kpi 800, tabular),
// unit, optional sparkline. `bind` makes the value a C-class text written by bindText (no React render, Tier S <= 4 Hz);
// `value` with cls "D" uses MotionNumber (pop-in of changed digits). Red text only when `hero` (one-red rule).
import * as React from 'react'
import { cn } from '@/lib/utils'
import { bindText } from '@/ui/motion/bindText'
import { MotionNumber } from '@/ui/motion/MotionNumber'
import { LfSparkline } from './LfSparkline'
import type { LfSeries } from './series'

export interface LfStatProps {
  label: string
  unit?: string
  format: (v: number) => string
  value?: number
  bind?: () => number
  stale?: () => number
  cls?: 'C' | 'D'
  spark?: LfSeries
  sparkTarget?: number
  hero?: boolean
  density?: 'hud' | 'editorial'
  className?: string
}

export function LfStat({ label, unit, format, value, bind, stale, cls = 'C', spark, sparkTarget, hero, density = 'hud', className }: LfStatProps) {
  const ref = React.useRef<HTMLSpanElement>(null)
  React.useEffect(() => {
    if (!bind || !ref.current) return
    return bindText(ref.current, bind, format, stale)
  }, [bind, format, stale])
  const kpi = density === 'hud' ? 'text-hud-kpi' : 'text-ed-kpi'
  return (
    <div data-lf-stat="" className={cn('flex items-end justify-between gap-2', className)}>
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="truncate text-hud-cap font-semibold uppercase text-muted-foreground">{label}</span>
        {/* a half-width space between number and unit, but % and the degree sign sit tight (AWR-14 §13.3 rule 1) */}
        <span className={cn('flex items-baseline', unit === '%' || unit === '°' ? 'gap-px' : 'gap-1')}>
          {bind ? (
            <span ref={ref} data-numeric="" className={cn(kpi, 'font-extrabold', hero && 'text-lf-hero-text')} />
          ) : cls === 'D' && value !== undefined ? (
            <MotionNumber value={value} format={format} className={cn(kpi, 'font-extrabold', hero && 'text-lf-hero-text')} />
          ) : (
            <span data-numeric="" className={cn(kpi, 'font-extrabold', hero && 'text-lf-hero-text')}>
              {value === undefined ? format(Number.NaN) : format(value)}
            </span>
          )}
          {/* a missing value reads as a lone dash, without its unit */}
          {unit && (bind || (value !== undefined && Number.isFinite(value))) ? <span className="text-hud-sub font-semibold text-muted-foreground">{unit}</span> : null}
        </span>
      </div>
      {spark ? <LfSparkline series={spark} target={sparkTarget} ariaLabel={label} className="mb-1.5" /> : null}
    </div>
  )
}
