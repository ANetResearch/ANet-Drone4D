// Histogram (M15-FR-070 F14; ADR-031; AWR-15 §9.3): counts of values over fixed bin edges (frame intervals: [0, 8.3,
// 16.7, 33.3, 50, 100, inf) ms), drawn as rung columns in the lieflat unit language (one rung = one honest unit, the
// automatic nice unit of LfBarRank), the median bin flagged in the source line by the caller. SVG, refreshed <= 1 Hz by
// its owner (the Perf panel recomputes the counts once per second).
import { LfBarRank, barUnit, type LfBarDatum } from './LfBarRank'

export interface LfHistogramProps {
  counts: readonly number[]
  edges: readonly number[]
  /** label of an edge interval, e.g. "8.3-16.7" */
  label?: (lo: number, hi: number) => string
  hero?: number | null
  height?: number
  format?: (v: number) => string
  ariaLabel: string
  className?: string
}

const edgeText = (v: number) => (Number.isFinite(v) ? String(v) : '')

/** bin counts of values (pure; exported for tests) */
export function binCounts(values: ArrayLike<number>, n: number, edges: readonly number[], out: number[] = []): number[] {
  out.length = edges.length - 1
  out.fill(0)
  for (let i = 0; i < n; i++) {
    const v = values[i]
    if (!Number.isFinite(v)) continue
    let b = edges.length - 2
    for (let k = 1; k < edges.length; k++) {
      if (v < edges[k]) {
        b = k - 1
        break
      }
    }
    out[b]++
  }
  return out
}

/** index of the bin holding the median (-1 when empty) */
export function medianBin(counts: readonly number[]): number {
  const total = counts.reduce((a, b) => a + b, 0)
  if (!total) return -1
  let acc = 0
  for (let i = 0; i < counts.length; i++) {
    acc += counts[i]
    if (acc * 2 >= total) return i
  }
  return counts.length - 1
}

export function LfHistogram({ counts, edges, label, hero = null, height = 150, format, ariaLabel, className }: LfHistogramProps) {
  const lab = label ?? ((lo: number, hi: number) => (Number.isFinite(hi) ? `${edgeText(lo)}-${edgeText(hi)}` : `>${edgeText(lo)}`))
  const data: LfBarDatum[] = counts.map((c, i) => ({ label: lab(edges[i], edges[i + 1]), value: c }))
  return <LfBarRank variant="rung" data={data} unit={barUnit(data)} hero={hero !== null && hero >= 0 ? data[hero]?.label : null} height={height} format={format} ariaLabel={ariaLabel} className={className} />
}
