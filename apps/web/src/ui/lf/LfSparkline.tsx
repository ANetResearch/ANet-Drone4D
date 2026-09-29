// Sparkline (M15-FR-070; d01 G17 reduced; g07 §5.1): CPU canvas 64 x 16, 1.25 px data line, last point as the hero dot,
// optional dashed target line (T*). Redrawn by the LfScheduler at `hz` (HUD 4) only when the series version changed; no
// React render per update. Streaming chart: subject to the Tier S slot cap.
import * as React from 'react'
import { cn } from '@/lib/utils'
import { setupCanvas, extent, drawEnvelope } from './canvas'
import { lfScheduler, type LfPrio } from './scheduler'
import { getLfTokens } from './useLfTokens'
import type { LfSeries } from './series'

export interface LfSparklineProps {
  series: LfSeries
  width?: number
  height?: number
  hz?: number
  prio?: LfPrio
  target?: number
  hero?: boolean
  ariaLabel: string
  className?: string
  onSlot?: (has: boolean) => void
}

export function LfSparkline({ series, width = 64, height = 16, hz = 4, prio = 2, target, hero = true, ariaLabel, className, onSlot }: LfSparklineProps) {
  const ref = React.useRef<HTMLCanvasElement>(null)
  React.useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const surf = setupCanvas(cv, width, height)
    if (!surf) return
    const ext = new Float64Array(2)
    return lfScheduler.add({
      el: cv, streaming: true, hz, prio, version: () => series.version(), onSlot,
      draw: () => {
        const { ctx } = surf
        const tok = getLfTokens()
        ctx.clearRect(0, 0, width, height)
        const n = series.len()
        if (n < 2) return
        const t0 = series.t(0)
        const t1 = series.t(n - 1)
        extent(series, t0, t1, ext)
        let lo = ext[0]
        let hi = ext[1]
        if (target !== undefined) {
          lo = Math.min(lo, target)
          hi = Math.max(hi, target)
        }
        if (hi - lo < 1e-9) {
          hi += 0.5
          lo -= 0.5
        }
        const x = (t: number) => ((t - t0) / Math.max(1e-9, t1 - t0)) * (width - 4) + 1
        const y = (v: number) => height - 2 - ((v - lo) / (hi - lo)) * (height - 4)
        if (target !== undefined) {
          ctx.save()
          ctx.setLineDash([2, 2])
          ctx.lineWidth = surf.hair
          ctx.strokeStyle = tok.faintdata
          ctx.beginPath()
          ctx.moveTo(0, y(target))
          ctx.lineTo(width, y(target))
          ctx.stroke()
          ctx.restore()
        }
        ctx.lineWidth = 1.25
        ctx.lineJoin = 'round'
        ctx.strokeStyle = tok.data
        drawEnvelope(ctx, series, t0, t1, x, y)
        ctx.fillStyle = hero ? tok.hero : tok.data
        ctx.beginPath()
        ctx.arc(x(t1), y(series.v(n - 1)), 1.75, 0, Math.PI * 2)
        ctx.fill()
      },
    })
  }, [series, width, height, hz, prio, target, hero, onSlot])
  return <canvas ref={ref} role="img" aria-label={ariaLabel} className={cn('lf-canvas', className)} style={{ width, height }} />
}
