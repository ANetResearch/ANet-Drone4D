// Ranked bars in the lieflat unit language (M15-FR-070; ADR-031): variant "rung" is F1 Rung Bars (columns built from
// horizontal rungs, one rung = one honest unit, a dot every fifth rung, the value on top), variant "ticks" is F5 Tick Rows
// (rows of vertical ticks, one tick = one unit, a dot every fifth tick, the value at the row end). The unit is automatic
// (niceStep(max / 40)) unless given; the source line should state it ("1 rung = 25.0K points"). The hero bar (key or
// "max") is red, every other bar is DATA ink; SVG, static or <= 2 Hz (lint LF-CHART-01: deterministic jitter).
import { cn } from '@/lib/utils'
import { useMotionTier } from '@/ui/motion/tier'
import { rnd } from './rnd'
import { niceStep } from './scale'

export interface LfBarDatum { label: string; value: number }
export interface LfBarRankProps {
  variant: 'rung' | 'ticks'
  data: readonly LfBarDatum[]
  unit?: number
  /** label of the hero bar, or 'max' for the largest value */
  hero?: string | null
  width?: number
  height?: number
  format?: (v: number) => string
  ariaLabel: string
  className?: string
}

const MAX_MARKS = 40

export function barUnit(data: readonly LfBarDatum[], unit?: number): number {
  if (unit && unit > 0) return unit
  let max = 0
  for (const d of data) if (d.value > max) max = d.value
  return niceStep(max / MAX_MARKS)
}

export function LfBarRank({ variant, data, unit, hero = null, width = 400, height = 220, format = String, ariaLabel, className }: LfBarRankProps) {
  const tier = useMotionTier()
  const fade = tier === 'full' || tier === 'lite' ? 'lf-fade' : undefined
  const u = barUnit(data, unit)
  let maxI = -1
  for (let i = 0; i < data.length; i++) if (maxI < 0 || data[i].value > data[maxI].value) maxI = i
  const heroI = hero === 'max' ? maxI : hero ? data.findIndex((d) => d.label === hero) : -1
  if (variant === 'rung') {
    const base = height - 26
    const step = Math.max(2, (base - 24) / MAX_MARKS)
    const colW = (width - 40) / Math.max(1, data.length)
    const hw = Math.min(14, colW / 2 - 4)
    return (
      <svg className={cn('lf-svg', className)} viewBox={`0 0 ${width} ${height}`} width="100%" role="img" aria-label={ariaLabel}>
        {data.map((d, i) => {
          const x = 20 + colW * (i + 0.5)
          const n = Math.max(0, Math.round(d.value / u))
          const ink = i === heroI ? 'var(--lf-hero)' : 'var(--lf-data)'
          const topY = base - Math.max(0, n - 1) * step
          return (
            <g key={d.label} className={fade}>
              {Array.from({ length: n }, (_, k) => {
                const y = base - k * step
                const w = hw - 1.5 + rnd(k + 1, i + 2) * 3
                return (
                  <g key={k} pointerEvents="none">
                    <line x1={x - w} x2={x + w} y1={y} y2={y} stroke={ink} strokeWidth={1} opacity={0.5 + rnd(k + 2, i + 4) * 0.5} />
                    {k % 5 === 4 ? <circle cx={x + hw + 4.5} cy={y} r={0.8} className="lf-faintdata" /> : null}
                  </g>
                )
              })}
              <text x={x} y={topY - 8} textAnchor="middle" fontSize={11} className={cn('lf-value', i === heroI && 'lf-hero')}>
                <title>{`${d.label} — ${format(d.value)}`}</title>
                {format(d.value)}
              </text>
              <text x={x} y={base + 16} textAnchor="middle" fontSize={7.5} fontWeight={700} letterSpacing=".08em" className="lf-axis">{d.label}</text>
            </g>
          )
        })}
        <line className="lf-grid" x1={8} x2={width - 8} y1={base + 4} y2={base + 4} />
      </svg>
    )
  }
  const rowH = Math.min(44, (height - 16) / Math.max(1, data.length))
  const x0 = 96
  const px = (width - x0 - 48) / MAX_MARKS
  return (
    <svg className={cn('lf-svg', className)} viewBox={`0 0 ${width} ${height}`} width="100%" role="img" aria-label={ariaLabel}>
      {data.map((d, i) => {
        const y = 12 + i * rowH + rowH / 2
        const n = Math.max(0, Math.round(d.value / u))
        const ink = i === heroI ? 'var(--lf-hero)' : 'var(--lf-data)'
        return (
          <g key={d.label} className={fade}>
            <text x={x0 - 10} y={y + 3} textAnchor="end" fontSize={8} fontWeight={700} letterSpacing=".08em" className="lf-label">{d.label}</text>
            <line className="lf-grid" x1={x0} x2={x0 + MAX_MARKS * px} y1={y + 6} y2={y + 6} strokeWidth={0.6} />
            {Array.from({ length: n }, (_, k) => {
              const x = x0 + k * px + px / 2
              const h = 9 + rnd(k + 1, i + 2) * 6
              return (
                <g key={k} pointerEvents="none">
                  <line x1={x} x2={x} y1={y + 6} y2={y + 6 - h} stroke={ink} strokeWidth={0.9} opacity={0.55 + rnd(k + 3, i + 5) * 0.45} />
                  {k % 5 === 4 ? <circle cx={x} cy={y + 10} r={0.8} className="lf-faintdata" /> : null}
                </g>
              )
            })}
            <text x={x0 + n * px + 10} y={y + 4} fontSize={11} className={cn('lf-value', i === heroI && 'lf-hero')}>
              <title>{`${d.label} — ${format(d.value)}`}</title>
              {format(d.value)}
            </text>
          </g>
        )
      })}
    </svg>
  )
}
export const LfRungBars = (p: Omit<LfBarRankProps, 'variant'>) => <LfBarRank variant="rung" {...p} />
export const LfTickRows = (p: Omit<LfBarRankProps, 'variant'>) => <LfBarRank variant="ticks" {...p} />
