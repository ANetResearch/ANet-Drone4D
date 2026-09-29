// Line chart in two modes (M15-FR-070; ADR-031): mode "live" is the G17 CPU canvas streaming line (window seconds, fixed
// domain, target and guide lines, STALE after staleMs without new samples, keyboard cursor with a nearest-sample readout);
// mode "static" is the F2 hairline line in SVG (calendar floor, hairline path, per-sample dots with hollow marks, the two
// highest peaks labelled with a halo, the hero peak in red). Aliases LfLiveLine and LfHairlineLine (ADR-031 names).
import * as React from 'react'
import { cn } from '@/lib/utils'
import { useMotionTier } from '@/ui/motion/tier'
import { setupCanvas, drawEnvelope } from './canvas'
import { lfScheduler, type LfPrio } from './scheduler'
import { getLfTokens } from './useLfTokens'
import { nearestIndex, type LfSeries } from './series'
import { linear } from './scale'

export interface LfLiveProps {
  mode: 'live'
  series: LfSeries
  windowSec?: number
  domain: readonly [number, number]
  target?: number
  guides?: readonly number[]
  staleMs?: number
  hz?: number
  prio?: LfPrio
  width: number
  height?: number
  ariaLabel: string
  format?: (v: number) => string
  className?: string
  onSlot?: (has: boolean) => void
}
export interface LfStaticPoint { t: number; v: number; hollow?: boolean }
export interface LfStaticProps {
  mode: 'static'
  data: readonly LfStaticPoint[]
  peaks?: number
  peakGap?: number
  hero?: 'peak' | 'last' | 'none'
  width?: number
  height?: number
  ariaLabel: string
  format?: (v: number) => string
  className?: string
}
export type LfLineProps = LfLiveProps | LfStaticProps

function LiveLine({ series, windowSec = 60, domain, target, guides, staleMs = 2000, hz = 10, prio = 1, width, height = 96, ariaLabel, format = String, className, onSlot }: LfLiveProps) {
  const ref = React.useRef<HTMLCanvasElement>(null)
  const cursor = React.useRef<number>(-1)
  const [readout, setReadout] = React.useState<string | null>(null)
  React.useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const surf = setupCanvas(cv, width, height)
    if (!surf) return
    const pad = 2
    return lfScheduler.add({
      el: cv, streaming: true, hz, prio, onSlot,
      version: () => series.version() * 4 + (cursor.current >= 0 ? 1 : 0),
      draw: (now) => {
        const { ctx, hair } = surf
        const tok = getLfTokens()
        ctx.clearRect(0, 0, width, height)
        const n = series.len()
        const tEnd = n ? series.t(n - 1) : 0
        const t0 = tEnd - windowSec * 1000
        const x = linear(t0, tEnd, pad, width - pad)
        const y = linear(domain[0], domain[1], height - pad, pad)
        ctx.lineWidth = hair
        ctx.strokeStyle = tok.grid
        ctx.beginPath()
        ctx.moveTo(0, height - pad + 0.5)
        ctx.lineTo(width, height - pad + 0.5)
        ctx.stroke()
        if (guides) {
          ctx.strokeStyle = tok.faintdata
          for (let i = 0; i < guides.length; i++) {
            ctx.beginPath()
            ctx.moveTo(0, y(guides[i]))
            ctx.lineTo(width, y(guides[i]))
            ctx.stroke()
          }
        }
        if (target !== undefined) {
          ctx.save()
          ctx.setLineDash([3, 3])
          ctx.strokeStyle = tok.faintdata
          ctx.beginPath()
          ctx.moveTo(0, y(target))
          ctx.lineTo(width, y(target))
          ctx.stroke()
          ctx.restore()
        }
        const stale = n > 0 && now - tEnd > staleMs // series times share the performance.now() base (LfSeries)
        ctx.lineWidth = 1.5
        ctx.lineJoin = 'round'
        ctx.strokeStyle = stale ? tok.faintdata : tok.data
        if (stale) ctx.setLineDash([2, 4])
        drawEnvelope(ctx, series, t0, tEnd, x, y)
        ctx.setLineDash([])
        if (cursor.current >= 0 && cursor.current < n) {
          const cx = Math.round(x(series.t(cursor.current))) + 0.5
          ctx.strokeStyle = tok.mut
          ctx.lineWidth = hair
          ctx.beginPath()
          ctx.moveTo(cx, 0)
          ctx.lineTo(cx, height)
          ctx.stroke()
        }
      },
    })
  }, [series, windowSec, domain, target, guides, staleMs, hz, prio, width, height, onSlot])

  const move = (dir: number) => {
    const n = series.len()
    if (!n) return
    cursor.current = cursor.current < 0 ? n - 1 : Math.max(0, Math.min(n - 1, cursor.current + dir))
    setReadout(`${format(series.v(cursor.current))}`)
    if (ref.current) lfScheduler.invalidate(ref.current)
  }
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowLeft') move(-1)
    else if (e.key === 'ArrowRight') move(1)
    else if (e.key === 'Escape') {
      cursor.current = -1
      setReadout(null)
      if (ref.current) lfScheduler.invalidate(ref.current)
    } else return
    e.preventDefault()
  }
  const onPointer = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const n = series.len()
    if (!n) return
    const r = e.currentTarget.getBoundingClientRect()
    const tEnd = series.t(n - 1)
    const t = tEnd - windowSec * 1000 + ((e.clientX - r.left) / r.width) * windowSec * 1000
    cursor.current = nearestIndex(series, t)
    setReadout(format(series.v(cursor.current)))
    lfScheduler.invalidate(e.currentTarget)
  }
  return (
    <div className={cn('relative', className)} onKeyDown={onKey}>
      <canvas ref={ref} role="img" aria-label={ariaLabel} className="lf-canvas" style={{ width, height }} onPointerMove={onPointer} />
      {readout !== null ? <span data-lf-readout="" className="pointer-events-none absolute top-0 right-0 text-hud-sub font-semibold">{readout}</span> : null}
    </div>
  )
}

function StaticLine({ data, peaks = 2, peakGap = 5, hero = 'peak', width = 400, height = 160, ariaLabel, format = String, className }: LfStaticProps) {
  const tier = useMotionTier()
  const animate = tier === 'full' || tier === 'lite'
  const n = data.length
  const padX = 12
  const base = height - 22
  let lo = Number.POSITIVE_INFINITY
  let hi = Number.NEGATIVE_INFINITY
  for (const p of data) {
    if (p.v < lo) lo = p.v
    if (p.v > hi) hi = p.v
  }
  if (!(hi > lo)) {
    hi = lo + 1
  }
  const x = (i: number) => padX + (n <= 1 ? 0 : (i / (n - 1)) * (width - 2 * padX))
  const y = linear(Math.min(0, lo), hi, base, 18)
  const order = [...data.keys()].sort((a, b) => data[b].v - data[a].v)
  const top: number[] = []
  for (const i of order) {
    if (top.every((t) => Math.abs(t - i) >= peakGap)) top.push(i)
    if (top.length >= peaks) break
  }
  const heroIdx = hero === 'peak' ? (top[0] ?? -1) : hero === 'last' ? n - 1 : -1
  const d = data.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`).join(' ')
  return (
    <svg className={cn('lf-svg', className)} viewBox={`0 0 ${width} ${height}`} width="100%" role="img" aria-label={ariaLabel}>
      <g aria-hidden="true" pointerEvents="none">
        {data.map((_, i) => <line key={`f${i}`} className="lf-floor" x1={x(i)} x2={x(i)} y1={base} y2={base - 7} />)}
        <line className="lf-grid" x1={padX / 2} x2={width - padX / 2} y1={base} y2={base} />
      </g>
      <path className={cn('lf-line', animate && 'lf-draw')} d={d} pathLength={1} strokeWidth={1} />
      {data.map((p, i) => {
        const big = top.includes(i)
        const isHero = i === heroIdx
        return (
          <circle key={`p${i}`} cx={x(i)} cy={y(p.v)} r={big ? 4.2 : 2.1}
            className={isHero ? 'lf-hero' : p.hollow ? 'lf-hollow' : 'lf-solid'}>
            <title>{`${format(p.v)}`}</title>
          </circle>
        )
      })}
      {top.map((i) => (
        <text key={`t${i}`} x={x(i)} y={y(data[i].v) - 11} textAnchor="middle" fontSize={9.5} className="lf-value lf-halo">{format(data[i].v)}</text>
      ))}
    </svg>
  )
}

export function LfLine(props: LfLineProps) {
  return props.mode === 'live' ? <LiveLine {...props} /> : <StaticLine {...props} />
}
export const LfLiveLine = (p: Omit<LfLiveProps, 'mode'>) => <LfLine mode="live" {...p} />
export const LfHairlineLine = (p: Omit<LfStaticProps, 'mode'>) => <LfLine mode="static" {...p} />
