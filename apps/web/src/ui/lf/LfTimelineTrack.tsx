// Timeline track (M15-FR-076; M12 §8.3, §8.4, §7.1; AWR-14 §6.17; AWR-15 §9.8): one CPU canvas redrawn by the LfScheduler
// (Tier S 4 Hz, B/A 10 Hz) only when the model version, the view, the width or the playhead changed. Layers bottom-up:
//   L0 track base;  L1 calendar floor (main, sub and tiny ticks under the lane baseline, main labels >= 64 px apart,
//   drawn whether or not something happened);  L2 the selected vehicle's altitude as 0.55 px hairlines per 3 px bucket
//   with a thin top outline (no area fill);  L3 range floors in a 4 px band above the baseline (rerun 45 degree hatch,
//   decimated dots, gap and stalled dashes) and the loaded range as a 2 px bottom bar;  L4 event markers, one per pixel
//   column (lifecycle DATA dot 5 px, route hollow dot 5 px, warning r500-stroke triangle 7 px, critical r500-stroke
//   octagon 7 px, the RedArbiter winner as the figure's only solid HERO dot 7 px, system FLOOR tick 6 px, superseded
//   records grey);  L5 bookmarks as the tl.bookmark path (10 px, 1.5 px stroke);  the playhead as a 1 px line.
// Canvas calls do not grow with the number of events (ColumnAgg per pixel column). The overview variant draws the
// density barcode of the whole range (three grey levels, a 2 px r500 stroke on columns with a critical), the view frame
// and a short playhead. The canvas never takes focus; interaction lives in ui/layout/TimelineTrackArea.
import * as React from 'react'
import { cn } from '@/lib/utils'
import { LF } from '@/lib/tokens/input.gen'
import { ICONS } from '@/ui/icons/registry'
import type { IconNode } from '@/ui/icons/types'
import { setupCanvas } from './canvas'
import { lfScheduler } from './scheduler'
import { getLfTokens, type LfTokens } from './useLfTokens'

/** the part of M12's TrackModel (engine/time/trackModel.ts) this renderer reads */
export interface TrackColumns { n: number; maxClass: Uint8Array; count: Uint32Array; repIdx?: Int32Array; maxLevel?: Uint8Array }
export interface TrackModelView {
  version: number
  columns(t0S: number, t1S: number, widthPx: number): TrackColumns
  series: { n: number; t: Float64Array; v: Float32Array }
  ranges: readonly { kind: 'rerun' | 'decimated' | 'gap' | 'stalled' | 'loaded'; t0S: number; t1S: number }[]
  ticks(spanS: number, widthPx: number): { main: number; sub?: number; tiny?: number }
  /** RedArbiter winner among unacknowledged critical markers (index into markers), -1 none (ADR-032) */
  heroIdx?: number
  markers?: { marker: Uint8Array }
  bookmarksS?: readonly number[]
}

// .evx marker classes (M12 engine/time/markers.ts MarkerClass); SUPERSEDED bit drawn grey
export const CLS_LIFECYCLE = 1
export const CLS_ROUTE = 2
export const CLS_WARNING = 3
export const CLS_CRITICAL = 4
export const CLS_SYSTEM = 5
const SUPERSEDED = 0x10

/** an empty model (version 0, no markers): tests and pages without a clock */
export function emptyTrackModel(): TrackModelView {
  const cols: TrackColumns = { n: 0, maxClass: new Uint8Array(0), count: new Uint32Array(0) }
  return {
    version: 0, columns: () => cols, series: { n: 0, t: new Float64Array(0), v: new Float32Array(0) }, ranges: [],
    ticks: (spanS) => ({ main: spanS > 600 ? 60 : spanS > 120 ? 10 : 5 }),
  }
}

/** label of a floor tick at tS seconds for a main step of stepS: 12:30, 1:02:00, 12:31.5 */
export function trackTimeLabel(tS: number, stepS: number): string {
  const neg = tS < 0
  const a = Math.abs(tS)
  const tenths = Math.round(a * 10)
  const s = Math.floor(tenths / 10)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  const mmss = `${h > 0 ? `${h}:${String(m).padStart(2, '0')}` : String(m)}:${String(ss).padStart(2, '0')}`
  return `${neg ? '-' : ''}${mmss}${stepS < 1 ? `.${tenths % 10}` : ''}`
}

/** lane geometry of a detail track of height h (CSS px): marker lane on top, floor and labels at the bottom */
export function trackLane(h: number, labels: boolean): { base: number; cy: number; labelY: number } {
  const labelH = labels ? 11 : 0
  const base = h - labelH - 1
  return { base, cy: Math.max(5, Math.round((base - 1) / 2)) + 0.5, labelY: h - 2 }
}

let bookmarkPath: Path2D | null = null
function bookmarkGlyph(): Path2D | null {
  if (bookmarkPath || typeof Path2D === 'undefined') return bookmarkPath
  const p = new Path2D()
  for (const [tag, attrs] of ICONS['tl.bookmark'] as IconNode) {
    if (tag === 'path' && typeof attrs.d === 'string') p.addPath(new Path2D(attrs.d))
  }
  bookmarkPath = p
  return p
}

function octagon(ctx: CanvasRenderingContext2D, cx: number, cy: number, r: number): void {
  for (let i = 0; i < 8; i++) {
    const ang = Math.PI / 8 + (i * Math.PI) / 4
    const px = cx + r * Math.cos(ang)
    const py = cy + r * Math.sin(ang)
    if (i === 0) ctx.moveTo(px, py)
    else ctx.lineTo(px, py)
  }
  ctx.closePath()
}

interface DrawState { view: { t0S: number; t1S: number }; playheadS: number; range: { t0S: number; t1S: number }; rev: number }

function drawDetail(ctx: CanvasRenderingContext2D, hair: number, tok: LfTokens, model: TrackModelView, st: DrawState, width: number, height: number, labels: boolean): void {
  const { t0S, t1S } = st.view
  const span = Math.max(1e-6, t1S - t0S)
  const x = (tS: number) => ((tS - t0S) / span) * width
  const { base, cy, labelY } = trackLane(height, labels)
  ctx.clearRect(0, 0, width, height)
  // L0 track base (the lane only; labels sit on the bar background)
  ctx.fillStyle = tok.track
  ctx.fillRect(0, 0, width, base)
  // L1 calendar floor: ticks hang below the baseline, labels under the main ticks
  const tk = model.ticks(span, width)
  const tickSet = (step: number | undefined, len: number) => {
    if (!step || !(step > 0)) return
    ctx.beginPath()
    for (let t = Math.ceil(t0S / step) * step; t <= t1S + 1e-9; t += step) {
      const px = Math.round(x(t)) + 0.5
      ctx.moveTo(px, base)
      ctx.lineTo(px, base + len)
    }
    ctx.stroke()
  }
  ctx.strokeStyle = tok.floor
  ctx.lineWidth = hair
  tickSet(tk.tiny, 2)
  tickSet(tk.sub, 3)
  tickSet(tk.main, labels ? 4 : 5)
  ctx.strokeStyle = tok.grid
  ctx.beginPath()
  ctx.moveTo(0, base + 0.5)
  ctx.lineTo(width, base + 0.5)
  ctx.stroke()
  if (labels && tk.main > 0) {
    ctx.fillStyle = tok.mut
    ctx.font = `10px ${tok.font}`
    ctx.textBaseline = 'alphabetic'
    let lastRight = Number.NEGATIVE_INFINITY
    for (let t = Math.ceil(t0S / tk.main) * tk.main; t <= t1S + 1e-9; t += tk.main) {
      const px = Math.round(x(t))
      const s = trackTimeLabel(t, tk.main)
      const w = ctx.measureText(s).width
      const left = Math.min(Math.max(px + 2, 0), width - w)
      if (left < lastRight + 6) continue
      ctx.fillText(s, left, labelY)
      lastRight = left + w
    }
  }
  // L2 selected vehicle altitude: one hairline per 3 px bucket from the lane floor to the bucket maximum
  const s = model.series
  if (s.n > 1) {
    let lo = Number.POSITIVE_INFINITY
    let hi = Number.NEGATIVE_INFINITY
    for (let i = 0; i < s.n; i++) {
      const tt = s.t[i] / 1000
      if (tt < t0S || tt > t1S) continue
      if (s.v[i] < lo) lo = s.v[i]
      if (s.v[i] > hi) hi = s.v[i]
    }
    if (hi >= lo) {
      const top = 2
      const range = Math.max(1, hi - Math.min(lo, 0))
      const yOf = (v: number) => base - 2 - ((v - Math.min(lo, 0)) / range) * (base - 2 - top)
      ctx.strokeStyle = tok.faintdata
      ctx.lineWidth = 0.55
      ctx.beginPath()
      let bucket = Number.NaN
      let bmax = 0
      const flush = () => {
        if (Number.isNaN(bucket)) return
        const px = bucket * 3 + 1.5
        ctx.moveTo(px, base - 0.5)
        ctx.lineTo(px, yOf(bmax))
      }
      for (let i = 0; i < s.n; i++) {
        const tt = s.t[i] / 1000
        if (tt < t0S || tt > t1S) continue
        const b = Math.floor(x(tt) / 3)
        if (b !== bucket) {
          flush()
          bucket = b
          bmax = s.v[i]
        } else if (s.v[i] > bmax) bmax = s.v[i]
      }
      flush()
      ctx.stroke()
    }
  }
  // L3 range floors (4 px band above the baseline) and L6 the loaded range (2 px bottom bar of the lane)
  for (const r of model.ranges) {
    const a = Math.max(0, x(r.t0S))
    const b = Math.min(width, x(r.t1S))
    if (b <= a) continue
    if (r.kind === 'loaded') {
      ctx.fillStyle = tok.faintdata
      ctx.fillRect(a, base - 2, b - a, 2)
    } else if (r.kind === 'gap' || r.kind === 'stalled') {
      ctx.strokeStyle = tok.faint
      ctx.lineWidth = 1
      ctx.setLineDash([4, 3])
      ctx.beginPath()
      ctx.moveTo(a, base - 2.5)
      ctx.lineTo(b, base - 2.5)
      ctx.stroke()
      ctx.setLineDash([])
    } else if (r.kind === 'rerun') {
      ctx.save()
      ctx.beginPath()
      ctx.rect(a, 0, b - a, base)
      ctx.clip()
      ctx.strokeStyle = tok.faint
      ctx.lineWidth = 1
      ctx.beginPath()
      for (let px = Math.floor(a / 6) * 6 - base; px < b; px += 6) {
        ctx.moveTo(px, base)
        ctx.lineTo(px + base, 0)
      }
      ctx.stroke()
      ctx.restore()
    } else {
      ctx.fillStyle = tok.faint
      for (let px = Math.ceil(a / 3) * 3; px < b; px += 3) ctx.fillRect(px, base - 3, 1, 1)
    }
  }
  // L4 markers, one per pixel column; only the RedArbiter winner is a solid HERO dot (one red per figure)
  const cols = model.columns(t0S, t1S, width)
  const hero = model.heroIdx ?? -1
  const mk = model.markers?.marker
  ctx.lineWidth = 1
  for (let c = 0; c < cols.n; c++) {
    const k = cols.maxClass[c]
    if (!k) continue
    const rep = cols.repIdx ? cols.repIdx[c] : -1
    const grey = rep >= 0 && mk ? (mk[rep] & SUPERSEDED) !== 0 : false
    const cx = c + 0.5
    if (rep >= 0 && rep === hero && !grey) {
      ctx.fillStyle = tok.hero
      ctx.beginPath()
      ctx.arc(cx, cy, 3.5, 0, Math.PI * 2)
      ctx.fill()
      continue
    }
    const ink = grey ? tok.faintdata : tok.data
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
        ctx.lineTo(cx + 3.5, cy + 2.6)
        ctx.lineTo(cx - 3.5, cy + 2.6)
        ctx.closePath()
      } else octagon(ctx, cx, cy, 3.5)
      ctx.stroke()
    }
  }
  // L5 bookmarks: the tl.bookmark glyph (10 px, 1.5 px stroke) at the top of the lane with a hairline down to the floor
  const glyph = bookmarkGlyph()
  for (const b of model.bookmarksS ?? []) {
    if (b < t0S || b > t1S) continue
    const px = Math.round(x(b)) + 0.5
    ctx.strokeStyle = tok.data2
    ctx.lineWidth = hair
    ctx.beginPath()
    ctx.moveTo(px, Math.min(10, base))
    ctx.lineTo(px, base)
    ctx.stroke()
    if (glyph && base >= 14) {
      ctx.save()
      ctx.translate(px - 5, 0)
      ctx.scale(10 / 24, 10 / 24)
      ctx.lineWidth = 1.5 * (24 / 10)
      ctx.lineJoin = 'round'
      ctx.stroke(glyph)
      ctx.restore()
    }
  }
  // playhead (live pins it to the right end, M12-FR-017: keep that column inside the canvas)
  if (st.playheadS >= t0S && st.playheadS <= t1S) {
    const ph = pxCol(x(st.playheadS), width)
    ctx.strokeStyle = tok.txt
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(ph, 0)
    ctx.lineTo(ph, base + 4)
    ctx.stroke()
  }
}

/** centre of the 1 px column at x, clamped inside a canvas of this width (exported for tests) */
export function pxCol(x: number, width: number): number {
  return Math.min(Math.max(0, Math.round(width) - 1), Math.max(0, Math.round(x))) + 0.5
}

function drawOverview(ctx: CanvasRenderingContext2D, hair: number, tok: LfTokens, model: TrackModelView, st: DrawState, width: number, height: number): void {
  const { t0S, t1S } = st.range
  const span = Math.max(1e-6, t1S - t0S)
  const x = (tS: number) => ((tS - t0S) / span) * width
  ctx.clearRect(0, 0, width, height)
  ctx.fillStyle = tok.track
  ctx.fillRect(0, 0, width, height)
  // density barcode: 1-2, 3-9, >= 10 events per column in three grey levels; a 2 px r500 stroke on critical columns
  const cols = model.columns(t0S, t1S, width)
  const mk = model.markers?.marker
  for (let c = 0; c < cols.n; c++) {
    const n = cols.count[c]
    if (!n) continue
    ctx.fillStyle = n >= 10 ? tok.data : n >= 3 ? tok.faintdata : tok.faint
    ctx.fillRect(c, 1, 1, height - 2)
    const rep = cols.repIdx ? cols.repIdx[c] : -1
    if (cols.maxClass[c] === CLS_CRITICAL && !(rep >= 0 && mk && (mk[rep] & SUPERSEDED) !== 0)) {
      ctx.strokeStyle = tok.hero
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(c + 0.5, 0)
      ctx.lineTo(c + 0.5, 2)
      ctx.stroke()
    }
  }
  // view frame (1 px ring stroke) and the playhead short line
  const a = Math.max(0, x(st.view.t0S))
  const b = Math.min(width, x(st.view.t1S))
  ctx.strokeStyle = tok.mut
  ctx.lineWidth = hair
  ctx.strokeRect(Math.round(a) + 0.5, 0.5, Math.max(2, Math.round(b - a) - 1), height - 1)
  const ph = pxCol(x(st.playheadS), width)
  ctx.strokeStyle = tok.txt
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(ph, 0)
  ctx.lineTo(ph, height)
  ctx.stroke()
}

export interface LfTimelineTrackProps {
  model: TrackModelView
  view: { t0S: number; t1S: number }
  playheadS: number
  width: number
  height?: number
  ariaLabel: string
  className?: string
  /** detail (default): markers and floor labels over the view; overview: density barcode of `range` with the view frame */
  variant?: 'detail' | 'overview'
  /** whole range of the overview variant */
  range?: { t0S: number; t1S: number }
  /** draw the main tick labels under the floor (detail) */
  labels?: boolean
}

export function LfTimelineTrack({ model, view, playheadS, width, height = 36, ariaLabel, className, variant = 'detail', range, labels = true }: LfTimelineTrackProps) {
  const ref = React.useRef<HTMLCanvasElement>(null)
  const state = React.useRef<DrawState>({ view, playheadS, range: range ?? view, rev: 0 })
  React.useLayoutEffect(() => {
    const st = state.current
    st.view = view
    st.playheadS = playheadS
    st.range = range ?? view
    st.rev++
  }, [view, playheadS, range])
  React.useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const surf = setupCanvas(cv, width, height)
    if (!surf) return
    return lfScheduler.add({
      el: cv, streaming: false, hz: LF.hudHz, prio: 2,
      // any change of the data, the HERO, the view, the range or the playhead redraws; the scheduler caps the rate
      version: () => model.version * 1_000_003 + state.current.rev,
      draw: () => {
        const tok = getLfTokens()
        if (variant === 'overview') drawOverview(surf.ctx, surf.hair, tok, model, state.current, width, height)
        else drawDetail(surf.ctx, surf.hair, tok, model, state.current, width, height, labels)
      },
    })
  }, [model, width, height, variant, labels])
  return <canvas ref={ref} role="img" aria-label={ariaLabel} data-figure={variant === 'overview' ? 'timeline-overview' : 'timeline-track'} className={cn('lf-canvas block', className)} style={{ width, height }} />
}
