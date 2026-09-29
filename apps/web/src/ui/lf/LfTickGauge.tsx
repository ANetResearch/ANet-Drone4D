// Tick gauge (M15-FR-070 F11; lieflat C7): 1 tick = 1% of `max`, inked up to the value, milestone dots every 25%, the
// value in the centre and the remainder below. `mini` is the 20-tick inline bar of the HUD (1 tick = 5%, AWR-14 §3.7).
// The HUD mini gauge is grey; red only through `hero` (one-red rule).
import { cn } from '@/lib/utils'
import { rnd } from './rnd'

export interface LfTickGaugeProps {
  value: number
  max?: number
  mini?: boolean
  center?: string
  remainder?: string
  hero?: boolean
  ariaLabel: string
  className?: string
}

export function LfTickGauge({ value, max = 100, mini, center, remainder, hero, ariaLabel, className }: LfTickGaugeProps) {
  const frac = Math.max(0, Math.min(1, max > 0 ? value / max : 0))
  const ink = hero ? 'var(--lf-hero)' : 'var(--lf-data)'
  if (mini) {
    const N = 20
    const on = Math.round(frac * N)
    return (
      <svg className={cn('lf-svg', className)} viewBox={`0 0 ${N * 4} 10`} width={N * 4} height={10} role="img" aria-label={ariaLabel}>
        {Array.from({ length: N }, (_, k) => (
          <line key={k} x1={k * 4 + 1.5} x2={k * 4 + 1.5} y1={10} y2={k < on ? 1 : 6} stroke={k < on ? ink : 'var(--lf-faint)'} strokeWidth={1} />
        ))}
      </svg>
    )
  }
  const cx = 200
  const cy = 190
  const R0 = 104
  const A0 = -195
  const SW = 210
  const pol = (r: number, deg: number): [number, number] => [cx + r * Math.cos((deg * Math.PI) / 180), cy + r * Math.sin((deg * Math.PI) / 180)]
  const inked = Math.round(frac * 100)
  return (
    <svg className={cn('lf-svg mx-auto max-w-100', className)} viewBox="0 0 400 260" width="100%" role="img" aria-label={ariaLabel}>
      {Array.from({ length: 100 }, (_, k) => {
        const a = A0 + (k / 100) * SW
        const on = k < inked
        const len = on ? 13 + rnd(k + 1, 3) * 6 : 5 + rnd(k + 1, 7) * 2.5
        const [x1, y1] = pol(R0, a)
        const [x2, y2] = pol(R0 + len, a)
        return <line key={k} x1={x1} y1={y1} x2={x2} y2={y2} stroke={on ? ink : 'var(--lf-faint)'} strokeWidth={on ? 1 : 0.6} />
      })}
      {[25, 50, 75, 100].map((m) => {
        const [dx, dy] = pol(R0 - 7, A0 + (m / 100) * SW)
        return <circle key={m} cx={dx} cy={dy} r={1} className="lf-faintdata" />
      })}
      <text x={cx} y={cy - 4} textAnchor="middle" fontSize={34} className="lf-value">{center ?? `${inked}%`}</text>
      {remainder ? <text x={cx} y={cy + 16} textAnchor="middle" fontSize={8} fontWeight={600} letterSpacing=".1em" className="lf-axis">{remainder}</text> : null}
    </svg>
  )
}
