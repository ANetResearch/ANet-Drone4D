// Point cloud telemetry (M05 §7.1 PointCloudStats, §7.6 window.__perf fields, M05-FR-051, FR-052; AWR-18 §4.2, §9).
// Owner: M05. The engine updates one preallocated PointCloudStats in the world phase; the __perf groups pc, cas and load
// are written in place through the structural PerfSink below (the M06 probe satisfies it). No allocation per frame.
import type { PointCloudStats } from '../types'
import { FREEZE_EXTERNAL, FREEZE_SHADER_COMPILE, FREEZE_WARMUP } from './CascadeController'

export interface PerfRing { readonly buf: Float64Array; n: number }
export function pushRing(r: PerfRing, v: number): void {
  r.buf[r.n & (r.buf.length - 1)] = v
  r.n++
}

/** the parts of window.__perf written by M05 (AWR-18 §9.2; M05 §7.6) */
export interface PerfSink {
  forced: null | { tier?: 'A' | 'B' | 'S'; allowFallback?: boolean; fixedB?: number; perfInject?: string }
  load: { ttfp: number; ttfpFirstPixel: number; switchMs: number; revealAt: number; firstScreenBytes?: number; warmupWaitMs?: number }
  cas: {
    index: number; B: number; lo: number; hi: number; B_floor: number; rungChanges: number; bounces: number; reversals: number; inBandAtMs: number
    frozenFrames: number; evals: number; B_ring: PerfRing; index_ring: PerfRing; atFloorSinceMs?: number; atCeilSinceMs?: number
  }
  pc: {
    drawn: number; limitedBy: 0 | 1 | 2 | 3 | 4; limitedByHist: Uint32Array; achievedErr: PerfRing; selectMs: PerfRing; drawn_ring: PerfRing; B_ring: PerfRing
    limitedBy_ring: PerfRing; budgetViolations: number; inflight: number; queued: number; failed: number; canceled: number; residentPts: number
    residentPeak: number; cpuCacheBytes: number; cpuCachePeak: number; downloadedBytes: number; uniqueBytes: number; uploadPtsMax: number
    progress: number; poolRows: number; clampedByCapacity: boolean; fillRate?: number; maxPxEff?: number; rsEff?: number; poolStalls?: number
    pageUtil?: number; [k: string]: unknown
  }
  quality?: { samples: { t: number; pose: Float64Array; mask: Uint8Array | null }[] }
  bench?: { mode: string; done: boolean; flightT: number }
}

/** freeze mask of the CAS (M05 §6.8.3): EXTERNAL (ctx.frozen, page hidden), SHADER_COMPILE, WARMUP */
export function casFreezeMask(o: FreezeInput): number {
  return (o.frozen || o.hidden ? FREEZE_EXTERNAL : 0) | (o.compiled ? FREEZE_SHADER_COMPILE : 0) | (o.warmupLeft > 0 ? FREEZE_WARMUP : 0)
}
export interface FreezeInput { frozen: boolean; hidden: boolean; compiled: boolean; warmupLeft: number }

/** 1 s exponential average of the fill rate on budget- or node-limited frames (M05 §3.3) */
export function fillEma(prev: number, sample: number, dtMs: number, tauMs: number): number {
  if (!(dtMs > 0)) return prev
  const a = 1 - Math.exp(-dtMs / tauMs)
  return prev + (sample - prev) * a
}

export function newStats(): PointCloudStats {
  return {
    phase: 'idle', progress: 0, drawn: 0, B: 0, Beff: 0, rungIndex: 0, rungName: '', manual: false, limitedBy: 'complete', achievedErrPx: 0, fillRate: 0,
    inflight: 0, queued: 0, failed: 0, canceled: 0, residentPts: 0, cpuCacheBytes: 0, pendingUploadPts: 0, clampedByCapacity: false, floorHeld: false,
    poolStalls: 0, pageUtil: 0, maxPxEff: 0, rsEff: 1, frozenMask: 0, levelCounts: new Int32Array(32), selectMs: 0, lo: 0, hi: 0, Bfloor: 0,
    error: null, downloadedBytes: 0, uniqueBytes: 0, uploadPtsMax: 0, minSpacingM: 0,
  }
}

/** idle values (no world open, M05 §7.1) */
export function resetStats(s: PointCloudStats): void {
  s.phase = 'idle'
  s.progress = 0
  s.drawn = 0
  s.limitedBy = 'complete'
  s.achievedErrPx = 0
  s.fillRate = 0
  s.inflight = 0
  s.queued = 0
  s.residentPts = 0
  s.cpuCacheBytes = 0
  s.pendingUploadPts = 0
  s.pageUtil = 0
  s.levelCounts.fill(0)
  s.minSpacingM = 0
}
