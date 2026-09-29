// FrameSampler and per-layer timing (M06-FR-074, FR-079; AWR-18 §6.3 item 3, §9.3). Owner: M06.
// Frame start: presentation interval of every executed rAF into frame.interval (and the flight time into frame.t);
// refresh-period estimate by a "fastest frame" EMA (fast down, slow up) exposed as refreshMs() for M05 and, on a change
// of more than 5 %, handed to the refresh listener (cas.setTarget on hardware tiers, g02 §6).
// Frame end: layer CPU times into layers[*].cpuMs with overBudgetFrames against the AWR-03 §3.8 budgets, render phase
// time into gpu.renderMs (> 50 ms counts gpu.renderOver50), our logic (all phases minus render) > 50 ms after the reveal
// counts loaf.oursOver50 (the loop part of the LoAF attribution). Zero allocation.
import { frameTiming, layerMs, PERF_LAYERS, type PerfLayerId } from '../loop'
import { LAYER_BUDGET_MS, pushRing, type AwrPerf } from './probe'

export interface FrameSamplerState {
  refreshMs: number
  lastNotified: number
  software: boolean
  /** bit i = PERF_LAYERS[i] over its budget in the last frame */
  overMask: number
  revealed: boolean
}

export const sampler: FrameSamplerState = { refreshMs: 16.7, lastNotified: 16.7, software: true, overMask: 0, revealed: false }
let refreshListener: ((refreshMs: number) => void) | null = null
const budgets = new Float64Array(PERF_LAYERS.map((k: PerfLayerId) => LAYER_BUDGET_MS[k]))

/** software device class: fixed 33.3 ms (Tier S 30 fps, ADR-044) */
export function setSoftware(on: boolean): void {
  sampler.software = on
  if (on) sampler.refreshMs = 33.3
}
export function refreshMs(): number {
  return sampler.software ? 33.3 : sampler.refreshMs
}
export function onRefreshChange(cb: ((refreshMs: number) => void) | null): void {
  refreshListener = cb
}

/** fastest-frame EMA: intervals below the estimate pull it down fast, longer ones raise it slowly (g02 §6) */
export function feedInterval(dt: number): void {
  if (sampler.software || !(dt > 2) || dt > 100) return
  const r = sampler.refreshMs
  sampler.refreshMs = dt < r ? r + 0.2 * (dt - r) : r + 0.002 * (dt - r)
  if (Math.abs(sampler.refreshMs - sampler.lastNotified) > 0.05 * sampler.lastNotified) {
    sampler.lastNotified = sampler.refreshMs
    refreshListener?.(sampler.refreshMs)
  }
}

export function frameStart(p: AwrPerf, nowMs: number): void {
  if (p.frame.count > 0 && p.frame.lastNow >= 0) {
    const dt = nowMs - p.frame.lastNow
    pushRing(p.frame.interval, dt)
    pushRing(p.frame.t, p.bench.flightT)
    feedInterval(dt)
  }
  p.frame.lastNow = nowMs
  p.frame.count++
}

export function frameEnd(p: AwrPerf): void {
  let mask = 0
  for (let i = 0; i < PERF_LAYERS.length; i++) {
    const L = p.layers[PERF_LAYERS[i]]
    const v = layerMs[i]
    pushRing(L.cpuMs, v)
    if (v > budgets[i]) {
      L.overBudgetFrames++
      mask |= 1 << i
    }
  }
  sampler.overMask = mask
  const r = frameTiming.renderMs
  pushRing(p.gpu.renderMs, r)
  if (r > 50) p.gpu.renderOver50++
  if (sampler.revealed) {
    pushRing(p.loaf.oursMs, frameTiming.oursMs)
    if (frameTiming.oursMs > 50) p.loaf.oursOver50++
  }
}
