// Series sources for streaming charts (M15-FR-073; d01 §3.6.3): zero-allocation accessors t(i), v(i) and a version counter.
// Times are milliseconds on the performance.now() base (the same clock as the rAF timestamps the scheduler passes to draw).
// LfRing keeps Float64 times and Float32 values (default capacity 1200 = 120 s at 10 Hz). seriesFromPerfRing reads the
// {buf, n} rings of window.__perf directly (AWR-18 §9.2, capacity 65536, index & 0xffff).
import { LF } from '@/lib/tokens/input.gen'

export interface LfSeries {
  len(): number
  t(i: number): number
  v(i: number): number
  version(): number
}

export class LfRing implements LfSeries {
  private readonly ts: Float64Array
  private readonly vs: Float32Array
  private head = 0
  private n = 0
  private ver = 0
  constructor(readonly cap: number = LF.ringCap) {
    this.ts = new Float64Array(cap)
    this.vs = new Float32Array(cap)
  }
  push(t: number, v: number): void {
    this.ts[this.head] = t
    this.vs[this.head] = v
    this.head = (this.head + 1) % this.cap
    if (this.n < this.cap) this.n++
    this.ver++
  }
  clear(): void {
    this.n = 0
    this.head = 0
    this.ver++
  }
  len(): number {
    return this.n
  }
  t(i: number): number {
    return this.ts[(this.head - this.n + i + this.cap) % this.cap]
  }
  v(i: number): number {
    return this.vs[(this.head - this.n + i + this.cap) % this.cap]
  }
  version(): number {
    return this.ver
  }
}

export interface PerfRing { buf: Float64Array | Float32Array; n: number }
/** values from a __perf ring; times from a parallel ring when given, otherwise the sample index. The rings have a
 * power-of-two capacity (65536 for frame rings, 4096 for the others, M06 engine/perf), so the index mask is length - 1. */
export function seriesFromPerfRing(r: PerfRing, tRing?: PerfRing): LfSeries {
  const mask = r.buf.length - 1
  const tMask = tRing ? tRing.buf.length - 1 : 0
  const count = () => Math.min(r.n, r.buf.length)
  return {
    len: count,
    v: (i) => r.buf[(r.n - count() + i) & mask],
    t: (i) => (tRing ? tRing.buf[(tRing.n - count() + i) & tMask] : i),
    version: () => r.n,
  }
}

/** p-quantile of the last k values of a __perf ring into a scratch buffer (k <= scratch.length; allocation free) */
export function ringQuantile(r: PerfRing, k: number, q: number, scratch: Float64Array): number {
  const m = Math.min(k, r.n, r.buf.length, scratch.length)
  if (m === 0) return Number.NaN
  const mask = r.buf.length - 1
  for (let i = 0; i < m; i++) scratch[i] = r.buf[(r.n - m + i) & mask]
  const view = scratch.subarray(0, m)
  view.sort()
  return view[Math.min(m - 1, Math.floor(q * m))]
}

/** binary search of the sample nearest to time t (cursor, tooltip) */
export function nearestIndex(s: LfSeries, t: number): number {
  let lo = 0
  let hi = s.len() - 1
  if (hi < 0) return -1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (s.t(mid) < t) lo = mid + 1
    else hi = mid
  }
  if (lo > 0 && Math.abs(s.t(lo - 1) - t) < Math.abs(s.t(lo) - t)) return lo - 1
  return lo
}
