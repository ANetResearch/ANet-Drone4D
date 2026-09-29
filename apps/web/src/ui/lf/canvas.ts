// CPU canvas setup and the M4 envelope (M15-FR-071, FR-073; d01 §3.6.1, §3.6.4, §3.6.5). Streaming charts always use
// getContext('2d', { willReadFrequently: true }) (CPU raster: SwiftShader GPU canvas drops 10 Hz charts to 5.6-7.4 fps;
// lint LF-CHART-02); backing store DPR capped at 2; hairline = 1 / dpr. The envelope keeps first/min/max/last per pixel
// column, so the drawn curve matches the data envelope exactly at any density.
import { LF } from '@/lib/tokens/input.gen'
import type { LfSeries } from './series'

export interface CanvasSurface { ctx: CanvasRenderingContext2D; dpr: number; hair: number; w: number; h: number }

export function setupCanvas(cv: HTMLCanvasElement, cssW: number, cssH: number): CanvasSurface | null {
  const dpr = Math.min(LF.canvasDprMax, globalThis.devicePixelRatio || 1)
  const bw = Math.max(1, Math.round(cssW * dpr))
  const bh = Math.max(1, Math.round(cssH * dpr))
  if (cv.width !== bw) cv.width = bw
  if (cv.height !== bh) cv.height = bh
  const ctx = cv.getContext('2d', { willReadFrequently: true, alpha: true })
  if (!ctx) return null
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  return { ctx, dpr, hair: 1 / dpr, w: cssW, h: cssH }
}

/**
 * Stroke series s over [t0, t1] with the per-column envelope. xOf and yOf map time and value to CSS px.
 * Returns the index of the last drawn sample (or -1). No allocation.
 */
export function drawEnvelope(ctx: CanvasRenderingContext2D, s: LfSeries, t0: number, t1: number, xOf: (t: number) => number, yOf: (v: number) => number): number {
  const n = s.len()
  let col = Number.NaN
  let first = 0
  let mn = 0
  let mx = 0
  let last = 0
  let started = false
  let lastIdx = -1
  ctx.beginPath()
  for (let i = 0; i < n; i++) {
    const t = s.t(i)
    if (t < t0 || t > t1) continue
    const v = s.v(i)
    if (!Number.isFinite(v)) continue
    const c = Math.floor(xOf(t))
    if (c !== col) {
      if (!Number.isNaN(col)) {
        const x = col + 0.5
        if (!started) {
          ctx.moveTo(x, yOf(first))
          started = true
        } else ctx.lineTo(x, yOf(first))
        ctx.lineTo(x, yOf(mn))
        ctx.lineTo(x, yOf(mx))
        ctx.lineTo(x, yOf(last))
      }
      col = c
      first = mn = mx = last = v
    } else {
      if (v < mn) mn = v
      if (v > mx) mx = v
      last = v
    }
    lastIdx = i
  }
  if (!Number.isNaN(col)) {
    const x = col + 0.5
    if (!started) ctx.moveTo(x, yOf(first))
    else ctx.lineTo(x, yOf(first))
    ctx.lineTo(x, yOf(mn))
    ctx.lineTo(x, yOf(mx))
    ctx.lineTo(x, yOf(last))
  }
  ctx.stroke()
  return lastIdx
}

/** min and max of the finite values in [t0, t1]; writes into out[0], out[1] (no allocation) */
export function extent(s: LfSeries, t0: number, t1: number, out: Float64Array): Float64Array {
  let lo = Number.POSITIVE_INFINITY
  let hi = Number.NEGATIVE_INFINITY
  const n = s.len()
  for (let i = 0; i < n; i++) {
    const t = s.t(i)
    if (t < t0 || t > t1) continue
    const v = s.v(i)
    if (!Number.isFinite(v)) continue
    if (v < lo) lo = v
    if (v > hi) hi = v
  }
  out[0] = lo
  out[1] = hi
  return out
}
