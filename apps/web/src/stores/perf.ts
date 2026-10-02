// Performance summary for the HUD and the Perf panel (M06 §6.18, FR-077, AC-053; AWR-14 §3.7). Owner: M06.
// Written by the governor-phase task 'perf-summary' at 4 Hz (Tier S) or 10 Hz (Tier B/A) from window.__perf: frame
// interval p50/p95 over the last 2 s, tier and device class, forced switches, CAS budget and rung, limitedBy, achieved
// error, load progress, requests in flight, PerfGovernor step and label key, motion cap (read by M15 motion tier as the
// governor input), over-budget layer mask, focus latency p95 and the backend state. Writes only when a value changed
// (store writes <= 4 Hz on Tier S, PERF-AC-020). Tier fields and backendState are set by the viewport host.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { frameSampler, governor, loop, perfProbe, tailRing } from '@/engine'
import { uiTickDue } from './uiTick'

export type Tier = 'A' | 'B' | 'S'
export type LimitedBy = 'complete' | 'budget' | 'pool' | 'bandwidth' | 'decode' | 'upload' | 'capacity' | 'nodes' | 'headroom' | 'error'
export type MotionCap = 'full' | 'lite' | 'reduced'
export interface PerfSummary {
  tier: Tier | null
  deviceClass: 'dGPU' | 'iGPU' | 'software' | null
  forced: boolean
  p50Ms: number
  p95Ms: number
  /** T*, the target frame interval of the tier (Tier S 33.3 ms) */
  targetMs: number
  B: number
  rung: number
  limitedBy: LimitedBy
  achievedErrPx: number
  progress: number
  inflight: number
  failed: number
  clampedByCapacity: boolean
  governorStep: number
  governorLabelKey: string | null
  motionCap: MotionCap | null
  layersOverMask: number
  latencyP95Ms: number
  focusLowLatency: boolean
  backendState: 'WARMING' | 'READY' | 'LOST' | 'FAILED'
  /** point size self test degraded to 1 px (M06-E004, HUD badge) */
  pointSizeDegraded: boolean
}

export const perfStore = createAwrStore<PerfSummary>('perf', () => ({
  tier: null, deviceClass: null, forced: false, p50Ms: Number.NaN, p95Ms: Number.NaN, targetMs: Number.NaN, B: 0, rung: 0,
  limitedBy: 'complete', achievedErrPx: Number.NaN, progress: 0, inflight: 0, failed: 0, clampedByCapacity: false,
  governorStep: 0, governorLabelKey: null, motionCap: null, layersOverMask: 0, latencyP95Ms: Number.NaN, focusLowLatency: false,
  backendState: 'WARMING', pointSizeDegraded: false,
}))

export function usePerf<T>(selector: (s: PerfSummary) => T): T {
  return useStore(perfStore, selector)
}

const WINDOW = 256
const buf = new Float64Array(WINDOW)
const lat = new Float64Array(WINDOW)
const LIMITED: readonly LimitedBy[] = ['budget', 'nodes', 'headroom', 'error', 'complete']

/** p-quantile of the first n values (in-place insertion sort of the scratch window, n <= 256) */
export function quantile(a: Float64Array, n: number, q: number): number {
  for (let i = 1; i < n; i++) {
    const v = a[i]
    let j = i - 1
    while (j >= 0 && a[j] > v) {
      a[j + 1] = a[j]
      j--
    }
    a[j + 1] = v
  }
  return n ? a[Math.min(n - 1, Math.floor(q * n))] : Number.NaN
}

const r1 = (v: number): number => Math.round(v * 10) / 10
const same = (a: number, b: number): boolean => a === b || (Number.isNaN(a) && Number.isNaN(b))

export function summarisePerf(): void {
  const p = perfProbe()
  // last 2 s of intervals (at most 256 frames)
  let n = tailRing(p.frame.interval, WINDOW, buf)
  let sum = 0
  let k = n - 1
  while (k >= 0 && sum < 2000) sum += buf[k--]
  const from = k + 1
  if (from > 0) {
    buf.copyWithin(0, from, n)
    n -= from
  }
  const p50 = r1(quantile(buf, n, 0.5))
  const p95 = r1(quantile(buf, n, 0.95))
  const ln = tailRing(p.latency.tSimToPixelMs, WINDOW, lat)
  const l95 = r1(quantile(lat, ln, 0.95))
  const g = governor()
  const s = perfStore.getState()
  const limitedBy = LIMITED[p.pc.limitedBy] ?? 'complete'
  const rung = p.cas.index
  const B = p.pc.drawn
  const progress = Math.round(p.pc.progress * 100) / 100
  const mask = frameSampler.overMask
  if (same(p50, s.p50Ms) && same(p95, s.p95Ms) && B === s.B && progress === s.progress && p.pc.inflight === s.inflight && p.pc.failed === s.failed &&
    limitedBy === s.limitedBy && rung === s.rung && g.step === s.governorStep && g.labelKey === s.governorLabelKey && mask === s.layersOverMask &&
    same(l95, s.latencyP95Ms) && p.pc.clampedByCapacity === s.clampedByCapacity) return
  perfStore.setState({
    p50Ms: p50, p95Ms: p95, B, progress, inflight: p.pc.inflight, failed: p.pc.failed, limitedBy, rung, governorStep: g.step,
    governorLabelKey: g.labelKey, layersOverMask: mask, latencyP95Ms: l95, clampedByCapacity: p.pc.clampedByCapacity,
  })
}

// published on the shared UI tick (Tier S 4 Hz, B/A 10 Hz; stores/uiTick.ts, ADR-066), in the governor phase of the tick frame
loop.register('governor', 'perf-summary.s', (ctx) => void (uiTickDue(ctx) && summarisePerf()), { tiers: ['S'] })
loop.register('governor', 'perf-summary.ba', (ctx) => void (uiTickDue(ctx) && summarisePerf()), { tiers: ['A', 'B'] })
