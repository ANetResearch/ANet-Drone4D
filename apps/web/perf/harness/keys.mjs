// Registered metric names of awr.perf.report.v1 (AWR-18 §11.2) and the CaseDef key mapping (M16 §6.7.3).
// A CaseDef metric key uses the dotted form ('frame.p95_ms'); replacing '.' with '_' must give a registered name.
// Module registries that prefix a namespace ('load.ttfp_ms', 'pc.failed_nodes') are accepted when the part after the
// first dot is registered (M05 perf/m05/cases.mjs). Keys that 18 has not registered yet are allowed only with
// `extra: true`: they are judged and written to result.json `extra`, never into the report (M16 §7.3.5, §14 item 21).

export const REGISTERED = new Set([
  'frame_p50_ms', 'frame_p95_ms', 'frame_p99_ms', 'frame_mean_ms', 'over50_pct', 'over100_pct', 'drop_pct', 'max_gap_ms',
  'ttfp_ms', 'ttfp_first_pixel_ms', 'switch_ms', 'tti_ms', 'points_avg', 'fill_rate_pct', 'hole_rate_pct', 'cas_switches',
  'cas_bounces', 'b_reversals_per_min', 'in_band_ms', 'converge_ms', 'failed_nodes', 'budget_violations',
  'resident_peak_pts', 'cpu_cache_peak_mb', 'dup_download_ratio', 'select_p95_ms', 'upload_max_pts', 'ours_over50',
  'main_js_p50_ms', 'sim_cpu_core', 'api_cpu_core', 'step_p99_us', 'rtf', 'tick_age_p99_ms', 'cmd_rtt_p99_ms',
  't_sim_to_pixel_p95_ms', 'cmd_to_visible_p95_ms', 'heap_growth_pct', 'gc_pct',
])
const LAYER_RE = /^layer_[a-zA-Z]+_ms$/

export function isRegistered(name) {
  return REGISTERED.has(name) || LAYER_RE.test(name)
}

/** report key (registered name) of a CaseDef metric key, or null when it cannot be mapped */
export function reportKey(key) {
  const flat = key.replaceAll('.', '_')
  if (isRegistered(flat)) return flat
  const i = key.indexOf('.')
  if (i > 0) {
    const tail = key.slice(i + 1).replaceAll('.', '_')
    if (isRegistered(tail)) return tail
  }
  return null
}

/** key used in result.json `extra` for an unregistered metric (snake case, unit suffix kept) */
export function extraKey(key) {
  return key.replaceAll('.', '_')
}
