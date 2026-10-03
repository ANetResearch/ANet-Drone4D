// window.__perf.time (M12 §10.3; NFR-020; AWR-18 §9). Owner: M12. One preallocated object updated in place every frame
// by the clock phase (and by the interpolation calls of the drones phase); installed under the M06 __perf root when the
// time runtime starts. Ratios are over a 1 s window, cost p95 over the last 600 samples (about 10 s at 60 fps).
import { TIME_PARAMS as P } from './params'

export interface PerfTime {
  simNowS: number
  tRenderS: number
  tFocusS: number
  dGlobalMs: number
  dFocusMs: number
  dWallMs: number
  hzEff: number
  jitterP95Ms: number
  rate: number
  state4: number
  epoch: number
  stale: boolean
  replay: boolean
  focusLowLatency: boolean
  /** focus exception active, blended in and D_focus within 10 % of its target (ADR-071; D1-AC-26 window start) */
  focusSettled: boolean
  clockSnaps: number
  unknownState: number
  holdRatio: number
  extrapRatio: number
  maxAgeMs: number
  ingestMs: number
  sampleMs: number
  sampleP95Ms: number
  clockMs: number
  clockP95Ms: number
  seekMs: number
  seekP95Ms: number
  bufferingMs: number
  timelineRedrawMs: number
  timelineRedraws: number
  /** frozen-state D decay switch (RK-M12-05; default on) */
  frozenZeroD: boolean
  frames: number
}

export const perfTime: PerfTime = {
  simNowS: 0, tRenderS: 0, tFocusS: 0, dGlobalMs: 0, dFocusMs: 0, dWallMs: 0, hzEff: 0, jitterP95Ms: 0,
  rate: 1, state4: 0, epoch: -1, stale: true, replay: false, focusLowLatency: false, focusSettled: false, clockSnaps: 0, unknownState: 0,
  holdRatio: 0, extrapRatio: 0, maxAgeMs: 0, ingestMs: 0, sampleMs: 0, sampleP95Ms: 0, clockMs: 0, clockP95Ms: 0,
  seekMs: Number.NaN, seekP95Ms: Number.NaN, bufferingMs: 0, timelineRedrawMs: 0, timelineRedraws: 0, frozenZeroD: true, frames: 0,
}

/** fixed-size cost window with an allocation-free p95 (insertion sort into a scratch copy, run at most 1 Hz) */
export class CostWindow {
  private readonly buf: Float64Array
  private readonly tmp: Float64Array
  private n = 0
  private head = 0
  constructor(cap: number = P.perfCostWindow) {
    this.buf = new Float64Array(cap)
    this.tmp = new Float64Array(cap)
  }
  push(v: number): void {
    this.buf[this.head] = v
    this.head = (this.head + 1) % this.buf.length
    if (this.n < this.buf.length) this.n++
  }
  p95(): number {
    const n = this.n
    if (n === 0) return 0
    const t = this.tmp
    for (let i = 0; i < n; i++) t[i] = this.buf[i]
    t.subarray(0, n).sort()
    return t[Math.min(n - 1, Math.floor(0.95 * n))]
  }
  reset(): void {
    this.n = 0
    this.head = 0
  }
}

/** 1 s window counters for holdRatio, extrapRatio and maxAgeMs (vehicles x frames) */
export class RatioWindow {
  private startMs = Number.NaN
  private seen = 0
  private hold = 0
  private extrap = 0
  private maxAge = 0
  add(seen: number, hold: number, extrap: number, maxAgeMs: number): void {
    this.seen += seen
    this.hold += hold
    this.extrap += extrap
    if (maxAgeMs > this.maxAge) this.maxAge = maxAgeMs
  }
  /** roll the window into perfTime when 1 s has elapsed */
  roll(nowMs: number, out: PerfTime): void {
    if (Number.isNaN(this.startMs)) this.startMs = nowMs
    if (nowMs - this.startMs < P.perfWindowMs) return
    out.holdRatio = this.seen > 0 ? this.hold / this.seen : 0
    out.extrapRatio = this.seen > 0 ? this.extrap / this.seen : 0
    out.maxAgeMs = this.maxAge
    this.startMs = nowMs
    this.seen = 0
    this.hold = 0
    this.extrap = 0
    this.maxAge = 0
  }
}

type PerfHost = { __perf?: Record<string, unknown> }
/** attach perfTime under window.__perf (the M06 root) once it exists; idempotent */
export function installPerfTime(root?: Record<string, unknown> | null): void {
  const r = root ?? (typeof window !== 'undefined' ? (window as unknown as PerfHost).__perf : undefined)
  if (r && r.time !== perfTime) r.time = perfTime
}
