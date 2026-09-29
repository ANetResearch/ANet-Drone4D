// Metric extraction (M16 §6.7.5; AWR-18 §2.5, §4.2, §9.3, §9.4). Pure functions over one run's artefacts:
//   snap    awr.perf.v1 snapshot written by the spec (rings exported as arrays; frame.interval and frame.t aligned)
//   server  server.json of the harness: { proc: { api: { cpu_core }, 'sim-core': { cpu_core } }, window: /api/sys/perf }
//   bench   awr.bench.result.v1 of a py case (records per N / rate / clients)
//   script  metrics.json written by a spec ({ <extract name>: number })
//   pytest  { passed, failed, skipped, errors }
// Each extractor returns a number or null (missing). Steady window: flight time t in (2, 60] s.
import { mean, quantile, sorted } from './judge.mjs'

export const STEADY = [2, 60]

export function steadyIntervals(snap, lo = STEADY[0], hi = STEADY[1]) {
  const iv = snap?.frame?.interval ?? []
  const t = snap?.frame?.t ?? []
  const out = []
  const n = Math.min(iv.length, t.length)
  for (let i = 0; i < n; i++) if (t[i] > lo && t[i] <= hi && Number.isFinite(iv[i])) out.push(iv[i])
  return out
}
function startIntervals(snap) {
  const iv = snap?.frame?.interval ?? []
  const t = snap?.frame?.t ?? []
  const out = []
  for (let i = 0; i < Math.min(iv.length, t.length); i++) if (t[i] >= 0 && t[i] <= 2) out.push(iv[i])
  return out
}
const pct = (a, f) => (a.length ? (100 * a.filter(f).length) / a.length : null)
const q = (a, p) => (a.length ? quantile(sorted(a), p) : null)
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const ring = (r) => (Array.isArray(r) ? r.filter((x) => Number.isFinite(x)) : [])

const frame = {
  frame_p50_ms: (s) => q(steadyIntervals(s), 0.5),
  frame_p95_ms: (s) => q(steadyIntervals(s), 0.95),
  frame_p99_ms: (s) => q(steadyIntervals(s), 0.99),
  frame_mean_ms: (s) => mean(steadyIntervals(s)),
  over50_pct: (s) => pct(steadyIntervals(s), (dt) => dt > 50),
  over100_pct: (s) => pct(steadyIntervals(s), (dt) => dt > 100),
  drop_pct: (s) => pct(steadyIntervals(s), (dt) => dt > 1.5 * (s?.meta?.targetMs ?? 33.3)),
  max_gap_ms: (s) => {
    const a = steadyIntervals(s)
    return a.length ? Math.max(...a) : null
  },
  start_gap_ms: (s) => {
    const a = startIntervals(s)
    return a.length ? Math.max(...a) : null
  },
}

export const extractors = {
  // ---- snapshot: frame pacing, load, CAS, streaming (18 §2.5, §4.2)
  ...Object.fromEntries(Object.entries(frame).map(([k, f]) => [k, { source: 'snapshot', fn: f }])),
  ttfp_ms: { source: 'snapshot', fn: (s) => num(s?.load?.ttfp) },
  load_ttfp_ms: { source: 'snapshot', fn: (s) => num(s?.load?.ttfp) },
  ttfp_first_pixel_ms: { source: 'snapshot', fn: (s) => num(s?.load?.ttfpFirstPixel) },
  switch_ms: { source: 'snapshot', fn: (s) => num(s?.load?.switchMs) },
  load_switch_ms: { source: 'snapshot', fn: (s) => num(s?.load?.switchMs) },
  tti_ms: { source: 'snapshot', fn: (s) => num(s?.load?.tti) },
  cas_switches: { source: 'snapshot', fn: (s) => num(s?.cas?.rungChanges) },
  cas_bounces: { source: 'snapshot', fn: (s) => num(s?.cas?.bounces) },
  b_reversals_per_min: { source: 'snapshot', fn: (s) => (num(s?.cas?.reversals) === null ? null : s.cas.reversals / (58 / 60)) },
  in_band_ms: { source: 'snapshot', fn: (s) => num(s?.cas?.inBandAtMs) },
  failed_nodes: { source: 'snapshot', fn: (s) => num(s?.pc?.failed) },
  pc_failed_nodes: { source: 'snapshot', fn: (s) => num(s?.pc?.failed) },
  budget_violations: { source: 'snapshot', fn: (s) => num(s?.pc?.budgetViolations) },
  pc_budget_violations: { source: 'snapshot', fn: (s) => num(s?.pc?.budgetViolations) },
  resident_peak_pts: { source: 'snapshot', fn: (s) => num(s?.pc?.residentPeak) },
  cpu_cache_peak_mb: { source: 'snapshot', fn: (s) => (num(s?.pc?.cpuCachePeak) === null ? null : s.pc.cpuCachePeak / 2 ** 20) },
  dup_download_ratio: { source: 'snapshot', fn: (s) => (s?.pc?.uniqueBytes > 0 ? s.pc.downloadedBytes / s.pc.uniqueBytes : null) },
  pc_dup_download_ratio: { source: 'snapshot', fn: (s) => (s?.pc?.uniqueBytes > 0 ? s.pc.downloadedBytes / s.pc.uniqueBytes : null) },
  select_p95_ms: { source: 'snapshot', fn: (s) => q(ring(s?.pc?.selectMs), 0.95) },
  upload_max_pts: { source: 'snapshot', fn: (s) => num(s?.pc?.uploadPtsMax) },
  fill_rate_pct: { source: 'snapshot', fn: (s) => (num(s?.pc?.fillRate) === null ? null : 100 * s.pc.fillRate) },
  points_avg: { source: 'snapshot', fn: (s) => mean(ring(s?.pc?.drawn_ring)) },
  ours_over50: { source: 'snapshot', fn: (s) => num(s?.loaf?.oursOver50) },
  main_js_p50_ms: { source: 'snapshot', fn: (s) => q(ring(s?.layers?.mainJs?.cpuMs), 0.5) },
  t_sim_to_pixel_p95_ms: { source: 'snapshot', fn: (s) => q(ring(s?.latency?.tSimToPixelMs), 0.95) },
  cmd_to_visible_p95_ms: { source: 'snapshot', fn: (s) => q(ring(s?.latency?.cmdToVisibleMs), 0.95) },
  cmd_to_visible_excess_ms: {
    source: 'snapshot',
    fn: (s) => {
      const v = q(ring(s?.latency?.cmdToVisibleMs), 0.95)
      return v === null ? null : v - (num(s?.latency?.dGlobalMs) ?? 0)
    },
  },
  hold_pct: { source: 'snapshot', fn: (s) => (s?.frame?.count > 0 ? (100 * (s.latency?.holdFrames ?? 0)) / s.frame.count : null) },
  worker_decode_p95_ms: { source: 'snapshot', fn: (s) => { const v = q(ring(s?.net?.decodeUs), 0.95); return v === null ? null : v / 1000 } },
  display_latency_p95_ms: { source: 'snapshot', fn: (s) => q(ring(s?.net?.ageMs), 0.95) },
  display_drift_ms: {
    source: 'snapshot',
    fn: (s) => {
      const a = ring(s?.net?.ageMs)
      if (a.length < 20) return null
      const k = Math.max(1, Math.floor(a.length / 6))
      return (quantile(sorted(a.slice(-k)), 0.5) ?? 0) - (quantile(sorted(a.slice(0, k)), 0.5) ?? 0)
    },
  },
  swarm_hz: { source: 'snapshot', fn: (s) => num(s?.net?.swarmHz) },
  unexpected_reconnects: { source: 'snapshot', fn: (s) => num(s?.net?.reconnects) },
  programs: { source: 'snapshot', fn: (s) => num(s?.gpu?.programs) },
  gl_errors: { source: 'snapshot', fn: (s) => num(s?.gpu?.glErrors) },
  react_commit_p95_ms: { source: 'snapshot', fn: (s) => q(ring(s?.ui?.react?.durations), 0.95) },
  // layer pairing (M06 bench.pairs.<group>.medianMs, 18 §5.2)
  ...Object.fromEntries(['drones', 'trails', 'environment', 'groundSky'].map((g) => [`layer_${g}_ms`,
    { source: 'snapshot', fn: (s) => num(s?.bench?.pairs?.[g]?.medianMs) }])),
  layers_fixed_total_ms: {
    source: 'snapshot',
    fn: (s) => {
      const p = s?.bench?.pairs
      if (!p) return null
      const v = ['drones', 'trails', 'environment', 'groundSky'].map((g) => num(p[g]?.medianMs))
      return v.some((x) => x === null) ? null : v.reduce((a, b) => a + b, 0)
    },
  },
  // ---- server: /proc sampling is authoritative for CPU (18 §9.4 item 3)
  sim_cpu_core: { source: 'server', fn: (_s, srv) => num(srv?.proc?.['sim-core']?.cpu_core) },
  api_cpu_core: { source: 'server', fn: (_s, srv) => num(srv?.proc?.api?.cpu_core) },
  tick_age_p99_ms: { source: 'server', fn: (_s, srv) => num(srv?.window?.['api.tick_age_p99_ms']?.p99 ?? srv?.window?.api?.tick_age_p99_ms?.p99) },
  step_p99_us: { source: 'server', fn: (_s, srv) => num(srv?.window?.['sim.step_p99_us']?.p99 ?? srv?.window?.sim?.step_p99_us?.p99) },
  step_max_us: { source: 'server', fn: (_s, srv) => num(srv?.window?.['sim.step_max_us']?.max ?? srv?.window?.sim?.step_max_us?.max) },
  rtf: { source: 'server', fn: (_s, srv) => num(srv?.window?.['sim.rtf']?.p50 ?? srv?.window?.sim?.rtf?.p50) },
  rss_growth_pct: { source: 'server', fn: (_s, srv) => num(srv?.rss_growth_pct) },
}

// ---- bench (awr.bench.result.v1): record selected by params.n (and rate, clients)
function record(bench, params) {
  const recs = bench?.records ?? []
  const n = Number(params?.judge_n ?? params?.n ?? 0)
  const sel = recs.filter((r) => (!n || r.n === n) && (params?.rate === undefined || Number(r.rate ?? 1) === Number(params.rate))
    && (params?.clients === undefined || Number(r.clients ?? 0) === Number(params.clients)))
  return sel.length ? sel[sel.length - 1] : null
}
export const benchExtractors = {
  rtf: (b, p) => num(record(b, p)?.rtf),
  sim_cpu_core: (b, p) => num(record(b, p)?.cpu_core),
  api_cpu_core: (b, p) => num(record(b, p)?.api_cpu_core),
  step_p99_us: (b, p) => num(record(b, p)?.step_us?.p99),
  step_max_us: (b, p) => num(record(b, p)?.step_us?.max),
  catchup_saturated: (b, p) => num(record(b, p)?.catchup_saturated),
  tick_age_p99_ms: (b, p) => num(record(b, p)?.tick_age_ms?.p99),
  publish_p99_us: (b, p) => num(record(b, p)?.publish_us?.p99 ?? record(b, p)?.publish_p99_us),
  cmd_rtt_p99_ms: (b, p) => num(record(b, p)?.cmd_rtt_ms?.p99 ?? record(b, p)?.rtt_ms?.p99),
  cmd_failures: (b, p) => num(record(b, p)?.cmd_failures ?? record(b, p)?.failures),
  event_gaps: (b, p) => num(record(b, p)?.event_gaps ?? record(b, p)?.gaps),
  swarm_hz_min: (b, p) => num(record(b, p)?.swarm_hz_min),
  credit_skips_pct: (b, p) => num(record(b, p)?.credit_skips_pct),
}

/**
 * Extract one metric of one run.
 * @param {{ key: string, source: string, extract?: string }} m
 * @param {{ snap?: object, server?: object, bench?: object, script?: object, pytest?: object }} art
 * @param {Record<string, unknown>} params
 */
export function extract(m, art, params = {}) {
  const name = (m.extract ?? m.key).replaceAll('.', '_')
  if (m.source === 'script') return num(art.script?.[name] ?? art.script?.[m.key])
  if (m.source === 'pytest') return num(art.pytest?.[name])
  if (m.source === 'bench') {
    const f = benchExtractors[name]
    return f ? f(art.bench, params) : null
  }
  const e = extractors[name]
  if (!e) return num(art.script?.[name])
  return e.fn(art.snap, art.server)
}

/** CPU cores used by a pid between two /proc/<pid>/stat samples (utime + stime in clock ticks) */
export function cpuCores(a, b, hz = 100) {
  if (!a || !b || b.t <= a.t) return null
  return (b.ticks - a.ticks) / hz / (b.t - a.t)
}
