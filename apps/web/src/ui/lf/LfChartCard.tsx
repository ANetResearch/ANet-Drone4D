// Chart card, the four-part lieflat card on shadcn Card (M15-FR-075; ADR-031; g07 §5.1-§5.2; AWR-15 §9.2):
// conclusion title, subtitle (legend and time range), figure, source line (upper case, tracked). HUD density: Card
// size="sm" (12 px padding), gap-2, rounded-xl, no border (mira draws a ring; a border would double it), text-hud-*.
// Editorial density: default Card, gap-3, rounded-4xl, text-ed-*. The root is a one-red figure (data-figure) and is
// focusable (arrow keys move the cursor of the charts inside). `paused` shows the Tier S streaming slot notice.
import * as React from 'react'
import { cn } from '@/lib/utils'
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Button } from '@/ui/components/ui/button'
import { useT } from '@/app/i18n'
import { lfScheduler } from './scheduler'

export type LfDensity = 'hud' | 'editorial'
export interface LfChartCardProps {
  title: React.ReactNode
  sub?: React.ReactNode
  src?: string
  action?: React.ReactNode
  density?: LfDensity
  figureId?: string
  wide?: boolean
  paused?: boolean
  className?: string
  children: React.ReactNode
}

export function LfChartCard({ title, sub, src, action, density = 'hud', figureId, wide, paused, className, children }: LfChartCardProps) {
  const t = useT()
  const hud = density === 'hud'
  const ref = React.useRef<HTMLDivElement>(null)
  return (
    <Card
      ref={ref}
      data-lf-card={density}
      data-figure={figureId}
      data-wide={wide ? '' : undefined}
      size={hud ? 'sm' : 'default'}
      tabIndex={0}
      onFocus={() => lfScheduler.focus(ref.current)}
      onBlur={() => lfScheduler.focus(null)}
      className={cn(hud ? 'gap-2 rounded-xl' : 'gap-3 rounded-4xl', 'outline-none focus-visible:ring-2 focus-visible:ring-ring', className)}
    >
      <CardHeader className="gap-0.5">
        <CardTitle className={hud ? 'text-hud-title font-semibold' : 'text-ed-title font-bold'}>{title}</CardTitle>
        {sub ? <CardDescription className={hud ? 'text-hud-sub' : 'text-ed-sub'}>{sub}</CardDescription> : null}
        {action ? <CardAction>{action}</CardAction> : null}
      </CardHeader>
      <CardContent className="relative">
        {children}
        {paused ? (
          <div data-lf-paused="" className="absolute inset-0 flex items-center justify-between gap-2 bg-card px-(--card-spacing)">
            <span className="text-hud-sub text-muted-foreground">{t('lf.paused')}</span>
            <Button size="xs" variant="outline" onClick={() => lfScheduler.focus(ref.current)}>
              {t('lf.switch')}
            </Button>
          </div>
        ) : null}
      </CardContent>
      {src ? (
        <CardFooter>
          <span data-lf-src="" className={cn('font-medium uppercase text-muted-foreground', hud ? 'text-hud-cap' : 'text-ed-axis')}>
            {src}
          </span>
        </CardFooter>
      ) : null}
    </Card>
  )
}
