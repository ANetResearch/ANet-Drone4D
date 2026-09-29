// Timeline track (M15-FR-076; M12 §6.5, §7.1; AWR-14 §6.17): one CPU canvas at 4 Hz drawing the L3 calendar floor, event
// markers aggregated per pixel column (the most severe shape wins: critical octagon ring, warning triangle, info tick),
// the selected vehicle's altitude as a hairline area, superseded ranges hatched, and the playhead. DOM nodes and canvas
// calls do not grow with the number of events (columns come from the model's ColumnAgg). Seeking is the shadcn Slider
// of the TimelineBar, not this canvas.
import * as React from 'react'
import { cn } from '@/lib/utils'
import { LF } from '@/lib/tokens/input.gen'
import { setupCanvas } from './canvas'
import { lfScheduler } from './scheduler'
import { getLfTokens } from './useLfTokens'

/** the part of M12's TrackModel (engine/time/trackModel.ts) this renderer reads */
export interface TrackColumns { n: number; maxClass: Uint8Array; count: Uint32Array; repIdx?: Int32Array }
export interface TrackModelView {
  version: number
  columns(t0S: number, t1S: number, widthPx: number): TrackColumns
  series: { n: number; t: Float64Array; v: Float32Array }
  ranges: readonly { kind: 'rerun' | 'decimated' | 'gap' | 'stalled' | 'loaded'; t0S: number; t1S: number }[]
  ticks(spanS: number, widthPx: number): { main: number; sub?: number }
  /** RedArbiter winner among unacknowledged critical markers (index into markers), -1 none (ADR-032) */
  heroIdx?: number
  markers?: { marker: Uint8Array }
  bookmarksS?: readonly number[]
}

// .evx marker classes (M12 engine/time/markers.ts MarkerClass); SUPERSEDED bit drawn grey
const CLS_LIFECYCLE = 1
const CLS_ROUTE = 2
const CLS_WARNING = 3
const CLS_CRITICAL = 4
const CLS_SYSTEM = 5
const SUPERSEDED = 0x10

/** an empty model until M12 provides engine/time/trackModel (version 0, no markers) */
export function emptyTrackModel(): TrackModelView {
  const cols: TrackColumns = { n: 0, maxClass: new Uint8Array(0), count: new Uint32Array(0) }
  return {
    version: 0, columns: () => cols, series: { n: 0, t: new Float64Array(0), v: new Float32Array(0) }, ranges: [],
    ticks: (spanS) => ({ main: spanS > 600 ? 60 : spanS > 120 ? 10 : 5 }),
  }
}

export interface LfTimelineTrackProps {
  model: TrackModelView
  view: { t0S: number; t1S: number }
  playheadS: number
  width: number
  height?: number
  ariaLabel: string
  className?: string
}

export function LfTimelineTrack({ model, view, playheadS, width, height = 36, ariaLabel, className }: LfTimelineTrackProps) {
  const ref = React.useRef<HTMLCanvasElement>(null)
  const state = React.useRef({ view, playheadS })
  React.useLayoutEffect(() => {
    state.current.view = view
    state.current.playheadS = playheadS
  }, [view, playheadS])
  React.useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const surf = setupCanvas(cv, width, height)
    if (!surf) return
    return lfScheduler.add({
      el: cv, streaming: false, hz: LF.hudHz, prio: 2,
      version: () => model.version * 1e6 + Math.round(state.current.playheadS * 10) + (state.current.view.t1S - state.current.view.t0S),
      draw: () => {
        const { ctx, hair } = surf
        const tok = getLfTokens()
        const { t0S, t1S } = state.current.view
        const span = Math.max(1e-6, t1S - t0S)
        const x = (tS: number) => ((tS - t0S) / span) * width
        ctx.clearRect(0, 0, width, height)
        const base = height - 6
        // L3 calendar floor: one hairline per main tick, whether or not something happened
        const { main } = model.ticks(span, width)
        ctx.strokeStyle = tok.floor
        ctx.lineWidth = hair
        ctx.beginPath()
        for (let t = Math.ceil(t0S / main) * main; t <= t1S; t += main) {
          const px = Math.round(x(t)) + 0.5
          ctx.moveTo(px, base)
          ctx.lineTo(px, base - 5)
        }
        ctx.stroke()
        ctx.strokeStyle = tok.grid
        ctx.beginPath()
        ctx.moveTo(0, base + 0.5)
        ctx.lineTo(width, base + 0.5)
        ctx.stroke()
        // selected vehicle altitude as a hairline area
        const s = model.series
        if (s.n > 1) {
          let hi = 1
          for (let i = 0; i < s.n; i++) if (s.v[i] > hi) hi = s.v[i]
          ctx.strokeStyle = tok.faintdata
          ctx.beginPath()
          for (let i = 0; i < s.n; i++) {
            const px = Math.round(x(s.t[i] / 1000)) + 0.5
            ctx.moveTo(px, base)
            ctx.lineTo(px, base - (s.v[i] / hi) * (base - 10))
          }
          ctx.stroke()
        }
        // ranges (M12 §8.3): rerun hatch, decimated dots, gap dashes, loaded 2 px floor bar
        for (const r of model.ranges) {
          const a = Math.max(0, x(r.t0S))
          const b = Math.min(width, x(r.t1S))
          if (b <= a) continue
          if (r.kind === 'loaded') {
            ctx.fillStyle = tok.track
            ctx.fillRect(a, height - 2, b - a, 2)
          } else if (r.kind === 'gap') {
            ctx.strokeStyle = tok.faint
            ctx.setLineDash([2, 2])
            ctx.beginPath()
            ctx.moveTo(a, base - 2.5)
            ctx.lineTo(b, base - 2.5)
            ctx.stroke()
            ctx.setLineDash([])
          } else {
            ctx.fillStyle = tok.floor
            for (let px = Math.ceil(a / 4) * 4; px < b; px += 4) ctx.fillRect(px, r.kind === 'rerun' ? 1 : base - 3, 1, r.kind === 'rerun' ? base - 2 : 1)
          }
        }
        // L4 markers, one per pixel column (M12 §8.3): lifecycle DATA dot, route hollow dot, warning r500-stroke
        // triangle, critical r500-stroke octagon, system FLOOR tick; only the RedArbiter winner is the HERO dot
        const cols = model.columns(t0S, t1S, width)
        const hero = model.heroIdx ?? -1
        const mk = model.markers?.marker
        const cy = Math.round(base / 2) + 0.5
        for (let c = 0; c < cols.n; c++) {
          const k = cols.maxClass[c]
          if (!k) continue
          const rep = cols.repIdx ? cols.repIdx[c] : -1
          const grey = rep >= 0 && mk ? (mk[rep] & SUPERSEDED) !== 0 : false
          const cx = c + 0.5
          if (rep >= 0 && rep === hero) {
            ctx.fillStyle = tok.hero
            ctx.beginPath()
            ctx.arc(cx, cy, 3.5, 0, Math.PI * 2)
            ctx.fill()
            continue
          }
          const ink = grey ? tok.faintdata : tok.data
          ctx.lineWidth = 1
          if (k === CLS_SYSTEM) {
            ctx.fillStyle = tok.floor
            ctx.fillRect(c, cy - 3, 1, 6)
          } else if (k === CLS_LIFECYCLE) {
            ctx.fillStyle = ink
            ctx.beginPath()
            ctx.arc(cx, cy, 2.5, 0, Math.PI * 2)
            ctx.fill()
          } else if (k === CLS_ROUTE) {
            ctx.strokeStyle = ink
            ctx.beginPath()
            ctx.arc(cx, cy, 2, 0, Math.PI * 2)
            ctx.stroke()
          } else if (k === CLS_WARNING || k === CLS_CRITICAL) {
            ctx.strokeStyle = grey ? tok.faintdata : tok.hero
            ctx.beginPath()
            if (k === CLS_WARNING) {
              ctx.moveTo(cx, cy - 3.5)
              ctx.lineTo(cx + 3.5, cy + 2.5)
              ctx.lineTo(cx - 3.5, cy + 2.5)
            } else {
              for (let i = 0; i < 8; i++) {
                const ang = Math.PI / 8 + (i * Math.PI) / 4
                const px = cx + 3.5 * Math.cos(ang)
                const py = cy + 3.5 * Math.sin(ang)
                if (i === 0) ctx.moveTo(px, py)
                else ctx.lineTo(px, py)
              }
            }
            ctx.closePath()
            ctx.stroke()
          }
        }
        // L5 bookmarks: a short DATA flag above the floor
        for (const b of model.bookmarksS ?? []) {
          if (b < t0S || b > t1S) continue
          const px = Math.round(x(b)) + 0.5
          ctx.strokeStyle = tok.data2
          ctx.beginPath()
          ctx.moveTo(px, 1)
          ctx.lineTo(px, 7)
          ctx.stroke()
        }
        // playhead
        const ph = Math.round(x(state.current.playheadS)) + 0.5
        ctx.strokeStyle = tok.data
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.moveTo(ph, 0)
        ctx.lineTo(ph, height)
        ctx.stroke()
      },
    })
  }, [model, width, height])
  return <canvas ref={ref} role="img" aria-label={ariaLabel} data-figure="timeline-track" className={cn('lf-canvas', className)} style={{ width, height }} />
}
