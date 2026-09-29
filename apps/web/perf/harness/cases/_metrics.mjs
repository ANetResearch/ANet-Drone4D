// MetricDef builders shared by the core case files (M16 §6.7.3; thresholds in perf/thresholds.json, AWR-18 §2.3).
// Helper module (leading '_'): the registry does not load it as a case list.

export const m = (key, unit, threshold, gating = true, o = {}) => ({ key, unit, source: o.source ?? 'snapshot',
  ...(o.extract ? { extract: o.extract } : {}), ...(threshold ? { threshold } : {}), gating: !!threshold && gating,
  ...(o.watch ? { dispersionWatch: true } : {}), ...(o.extra ? { extra: true } : {}) })

/** frame pacing of scene=pc (18 §2.3 left column; D1-AC-03a) */
export const framePc = (gating = true) => [
  m('frame.p50_ms', 'ms', 'frame_p50_pc', gating), m('frame.p95_ms', 'ms', 'frame_p95_pc', gating),
  m('frame.p99_ms', 'ms', 'frame_p99_pc', gating), m('over50_pct', 'pct', 'over50_pc', gating, { watch: true }),
  m('over100_pct', 'pct', 'over100_pc', gating), m('max_gap_ms', 'ms', 'max_gap', gating), m('frame.mean_ms', 'ms', null, false, { watch: true }),
  m('drop_pct', 'pct', null, false), m('start_gap_ms', 'ms', 'start_gap', false, { extra: true }),
]
/** frame pacing of scene=full (provisional, D1-AC-03b) */
export const frameFull = (gating = true) => [
  m('frame.p50_ms', 'ms', 'frame_p50_full', gating), m('frame.p95_ms', 'ms', 'frame_p95_full', gating),
  m('frame.p99_ms', 'ms', 'frame_p99_full', gating), m('over50_pct', 'pct', 'over50_full', gating, { watch: true }),
  m('over100_pct', 'pct', 'over100_full', gating), m('max_gap_ms', 'ms', 'max_gap', gating), m('frame.mean_ms', 'ms', null, false, { watch: true }),
  m('start_gap_ms', 'ms', 'start_gap', false, { extra: true }),
]
/** first screen (D1-AC-02) */
export const firstScreen = (gating = true) => [m('ttfp_ms', 'ms', 'ttfp_ms', gating, { watch: true }), m('ttfp_first_pixel_ms', 'ms', null),
  m('tti_ms', 'ms', 'tti_ms', false)]
/** CAS behaviour (D1-AC-04) */
export const cas = (gating = true) => [m('cas_switches', 'count', 'cas_switches', gating), m('cas_bounces', 'count', 'cas_bounces', gating),
  m('b_reversals_per_min', 'per_min', 'b_reversals_per_min', gating), m('in_band_ms', 'ms', 'in_band_ms', gating)]
/** bounded streaming (D1-AC-06); dup download ratio only for shenzhen, shanghai, suzhou */
export const streaming = (gating = true, dup = true) => [
  m('failed_nodes', 'count', 'failed_nodes', gating), m('budget_violations', 'count', 'budget_violations', gating),
  m('cpu_cache_peak_mb', 'bytes', 'cpu_cache_peak_mb', gating), m('select_p95_ms', 'ms', 'select_p95_ms', gating),
  m('upload_max_pts', 'count', 'upload_max_pts', gating), m('ours_over50', 'count', 'ours_over50', gating),
  m('dup_download_ratio', 'ratio', dup ? 'dup_download_ratio' : null, gating), m('resident_peak_pts', 'count', null),
  m('fill_rate_pct', 'pct', 'fill_rate_pct', false), m('points_avg', 'count', null),
]
/** server side of a live run (recorded; the gating CPU cases are fleet-ladder and gw-3clients) */
export const server = () => [m('sim_cpu_core', 'core', null, false, { source: 'server' }), m('api_cpu_core', 'core', null, false, { source: 'server' }),
  m('tick_age_p99_ms', 'ms', null, false, { source: 'server' }), m('step_p99_us', 'us', null, false, { source: 'server' })]

export const CITIES = ['shenzhen', 'newyork', 'shanghai', 'suzhou', 'sanfrancisco', 'chicago']

