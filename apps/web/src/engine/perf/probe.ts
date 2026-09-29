// window.__perf probe, schema awr.perf.v1 (AWR-18 §9.2-§9.3; M06 §6.18, FR-073). Owner: M06.
// One preallocated object: every ring is a fixed Float64Array (capacity 65536, write position n & mask), every scalar is
// updated in place, loaf.worst (8) and governor.history (64) reuse pooled entries. snapshot() is the only allocating
// method (harness and /bench only). inject() exists only in test builds (TEST switch folded at build time); the
// production object has no inject property (M06-AC-010). Field writers follow the ownership table of M06 §6.18: M06
// meta/forced/gpu/frame/layers/latency/loaf/governor/bench, M05 load.ttfp/switchMs, cas, pc, quality, M11 net, M15 ui.
import { PERF_LAYERS, type PerfLayerId } from '../loop'

export interface Ring { readonly buf: Float64Array; n: number }
export const RING_CAP = 65536
export const ring = (cap = RING_CAP): Ring => ({ buf: new Float64Array(cap), n: 0 })
export function pushRing(r: Ring, v: number): void {
  r.buf[r.n & (r.buf.length - 1)] = v
  r.n++
}
/** last k values of a ring into out (newest last); returns the count */
export function tailRing(r: Ring, k: number, out: Float64Array): number {
  const m = Math.min(k, r.n, r.buf.length, out.length)
  const mask = r.buf.length - 1
  for (let i = 0; i < m; i++) out[i] = r.buf[(r.n - m + i) & mask]
  return m
}
export function lastOf(r: Ring): number {
  return r.n > 0 ? r.buf[(r.n - 1) & (r.buf.length - 1)] : Number.NaN
}

export interface LoafWorst { durationMs: number; oursMs: number; renderMs: number; invoker: string }
export interface GovernorEntry { t: number; step: number; dir: -1 | 1; reason: string }
export interface LayerStat { cpuMs: Ring; draws: number; verts: number; overBudgetFrames: number }
export type ForcedFlags = { tier?: 'A' | 'B' | 'S'; allowFallback?: boolean; fixedB?: number; perfInject?: string }

/** Tier S budgets of AWR-03 §3.8 / AWR-18 §5.1 per perf layer (ms), used for overBudgetFrames and the HUD mask */
export const LAYER_BUDGET_MS: Readonly<Record<PerfLayerId, number>> = {
  pointcloud: 16, drones: 2.5, trails: 1, frustums: 1, environment: 2.5, groundSky: 1, labels: 2, hudCharts: 1, mainJs: 4,
}

export const GOVERNOR_HISTORY_CAP = 64
const SMALL = 4096

export function makeProbe(withInject: boolean) {
  const layers = {} as Record<PerfLayerId, LayerStat>
  for (const k of PERF_LAYERS) layers[k] = { cpuMs: ring(), draws: 0, verts: 0, overBudgetFrames: 0 }
  const worstPool: LoafWorst[] = Array.from({ length: 8 }, () => ({ durationMs: 0, oursMs: 0, renderMs: 0, invoker: '' }))
  const histPool: GovernorEntry[] = Array.from({ length: GOVERNOR_HISTORY_CAP }, () => ({ t: 0, step: 0, dir: 1 as -1 | 1, reason: '' }))
  const probe = {
    schema: 'awr.perf.v1' as const,
    meta: {
      build: '', gitSha: '', mode: 'production' as 'production' | 'test' | 'profiling', tier: 'S' as 'A' | 'B' | 'S',
      deviceClass: 'software' as 'software' | 'iGPU' | 'dGPU', renderer: '', adapterArch: '', crossOriginIsolated: false,
      canvasCss: [0, 0] as [number, number], drawingBuffer: [0, 0] as [number, number], dpr: 0.5, renderScale: 1, targetMs: 33.3,
      tailK: 2, worldId: '', contentVersion: '', scene: '', warmupMs: Number.NaN, microbenchMs: Number.NaN, backendState: 'WARMING',
    },
    forced: null as null | ForcedFlags,
    frame: { interval: ring(), t: ring(), count: 0, lastNow: -1 },
    load: { ttfp: Number.NaN, ttfpFirstPixel: Number.NaN, switchMs: Number.NaN, tti: Number.NaN, revealAt: 0, firstScreenBytes: 0, warmupWaitMs: 0 },
    cas: {
      index: 0, B: 0, lo: 0, hi: 0, B_floor: 0, rungChanges: 0, bounces: 0, reversals: 0, inBandAtMs: Number.NaN, frozenFrames: 0, evals: 0,
      B_ring: ring(SMALL), index_ring: ring(SMALL), atFloorSinceMs: Number.NaN, atCeilSinceMs: Number.NaN,
    },
    pc: {
      drawn: 0, limitedBy: 4 as 0 | 1 | 2 | 3 | 4, limitedByHist: new Uint32Array(5), achievedErr: ring(SMALL), selectMs: ring(SMALL),
      drawn_ring: ring(SMALL), B_ring: ring(SMALL), limitedBy_ring: ring(SMALL), budgetViolations: 0, inflight: 0, queued: 0, failed: 0,
      canceled: 0, residentPts: 0, residentPeak: 0, cpuCacheBytes: 0, cpuCachePeak: 0, downloadedBytes: 0, uniqueBytes: 0, uploadPtsMax: 0,
      progress: 0, poolRows: 0, clampedByCapacity: false, fillRate: 0, maxPxEff: 0, rsEff: 1, poolStalls: 0, pageUtil: 0,
    },
    layers,
    gpu: {
      programs: 0, calls: 0, passPlan: 0, rtAllocs: 0, glErrors: 0, contextLost: 0, renderMs: ring(), renderOver50: 0,
      /** programs at the reveal of the mask; later growth is M06-E006 */
      programsAtReveal: -1, planMismatches: 0, compiledAfterReveal: 0,
    },
    latency: { tSimToPixelMs: ring(SMALL), cmdToVisibleMs: ring(SMALL), focusJumpM: ring(SMALL), holdFrames: 0, extrapFrames: 0, dGlobalMs: 0 },
    net: {
      swarmHz: 0, focusHz: 0, selectedHz: 0, decodeUs: ring(SMALL), ageMs: ring(SMALL), creditSkips: 0, reconnects: 0, epoch: 0, eventGaps: 0,
      rttMs: Number.NaN, clockOffsetMs: Number.NaN, bytesPerS: 0,
    },
    ui: undefined as unknown,
    loaf: { count: 0, blockingMs: 0, oursOver50: 0, oursMs: ring(SMALL), worst: [] as LoafWorst[] },
    governor: { step: 0, history: [] as GovernorEntry[], state: 'NOMINAL' as 'NOMINAL' | 'DEGRADED' | 'FLOOR_RELEASED' },
    quality: { samples: [] as { t: number; pose: Float64Array; mask: Uint8Array | null }[] },
    bench: { mode: '' as '' | 'flight60' | 'layers' | 'converge', done: false, flightT: -1, pairs: {} as Record<string, unknown> },
    marks: {} as Record<string, number>,
    /** internal pools (not part of the schema; skipped by snapshot) */
    _pools: { worst: worstPool, hist: histPool, histN: 0 },
    /** the only allocating method (harness, /bench): rings as { n, values? } (rings: true exports the stored values) */
    snapshot(o: { rings?: boolean } = {}): Record<string, unknown> {
      const conv = (v: unknown): unknown => {
        if (v && typeof v === 'object' && 'buf' in (v as Ring) && (v as Ring).buf instanceof Float64Array) {
          const r = v as Ring
          const m = Math.min(r.n, r.buf.length)
          if (!o.rings) return []
          const out = new Float64Array(m)
          tailRing(r, m, out)
          return Array.from(out)
        }
        if (v instanceof Uint32Array || v instanceof Float64Array || v instanceof Uint8Array) return Array.from(v)
        if (Array.isArray(v)) return v.map(conv)
        if (typeof v === 'number') return Number.isFinite(v) ? v : null
        if (v && typeof v === 'object') {
          const out: Record<string, unknown> = {}
          for (const [k, x] of Object.entries(v)) if (typeof x !== 'function' && k !== '_pools') out[k] = conv(x)
          return out
        }
        return v
      }
      const s = conv(this) as Record<string, unknown>
      // ring counts next to the arrays (schema allows additional properties)
      const f = this.frame
      ;(s.frame as Record<string, unknown>).intervalN = f.interval.n
      return s
    },
    /** zero the rings and counters of a scope (harness between runs) */
    reset(scope: 'all' | 'frame' | 'pc' | 'net' | 'ui' | 'latency' = 'all'): void {
      if (scope === 'all' || scope === 'frame') {
        this.frame.interval.n = 0
        this.frame.t.n = 0
        this.frame.count = 0
        this.frame.lastNow = -1
        this.gpu.renderMs.n = 0
        this.gpu.renderOver50 = 0
        for (const k of PERF_LAYERS) {
          this.layers[k].cpuMs.n = 0
          this.layers[k].overBudgetFrames = 0
        }
        this.loaf.count = 0
        this.loaf.blockingMs = 0
        this.loaf.oursOver50 = 0
        this.loaf.oursMs.n = 0
        this.loaf.worst.length = 0
      }
      if (scope === 'all' || scope === 'latency') {
        const l = this.latency
        l.tSimToPixelMs.n = 0
        l.cmdToVisibleMs.n = 0
        l.focusJumpM.n = 0
        l.holdFrames = 0
        l.extrapFrames = 0
      }
      if (scope === 'all' || scope === 'net') {
        this.net.decodeUs.n = 0
        this.net.ageMs.n = 0
      }
      if (scope === 'all' || scope === 'pc') {
        this.pc.budgetViolations = 0
        this.pc.limitedByHist.fill(0)
      }
    },
    mark(name: string): void {
      this.marks[name] = performance.now()
    },
  }
  if (withInject) {
    ;(probe as unknown as { inject: (o: { busyMs?: number; renderBusyMs?: number }) => void }).inject = (o) => {
      INJECT.busyMs = Math.max(0, o.busyMs ?? 0)
      INJECT.renderBusyMs = Math.max(0, o.renderBusyMs ?? 0)
    }
  }
  return probe
}
export type AwrPerf = ReturnType<typeof makeProbe> & { inject?: (o: { busyMs?: number; renderBusyMs?: number }) => void }

/** busy-wait injection of AWR-18 §4.7 / §6.3 (test builds only; zero in production) */
export const INJECT = { busyMs: 0, renderBusyMs: 0 }
export function busyWait(ms: number): void {
  if (ms <= 0) return
  const end = performance.now() + ms
  while (performance.now() < end) {
    /* spin */
  }
}

/** push a governor history entry from the pool (at most 64 kept, the oldest dropped) */
export function pushGovernorHistory(p: AwrPerf, t: number, step: number, dir: -1 | 1, reason: string): void {
  const pools = p._pools
  const e = pools.hist[pools.histN++ % GOVERNOR_HISTORY_CAP]
  e.t = t
  e.step = step
  e.dir = dir
  e.reason = reason
  const h = p.governor.history
  if (h.length >= GOVERNOR_HISTORY_CAP) h.shift()
  h.push(e)
}

/** keep the 8 worst LoAF entries by duration (pooled objects) */
export function pushLoafWorst(p: AwrPerf, durationMs: number, oursMs: number, renderMs: number, invoker: string): void {
  const w = p.loaf.worst
  if (w.length === 8 && w[7].durationMs >= durationMs) return
  let e: LoafWorst
  if (w.length < 8) {
    e = p._pools.worst[w.length]
    w.push(e)
  } else e = w[7]
  e.durationMs = durationMs
  e.oursMs = oursMs
  e.renderMs = renderMs
  e.invoker = invoker
  w.sort((a, b) => b.durationMs - a.durationMs)
}
