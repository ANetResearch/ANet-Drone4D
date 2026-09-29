// Judging (M16-FR-044, FR-045, FR-046; AWR-18 §2.5, §3.1, §11.3, §12.3, §17). Pure functions, unit tested by
// apps/web/tests/m16/judge.test.ts (M16-AC-024).
//   * quantiles use the nearest-rank rule of 18 §2.5: sorted[min(n - 1, floor(p * n))];
//   * a metric is judged on the median of its runs; dispersion (max - min) / median > 25 % raises WARN (PERF-E013)
//     for the key metrics of PR-10;
//   * frame-pacing thresholds only warn when the in-run loadavg max exceeds 12.8 (PR-4, PERF-E012); quality, point
//     count and CPU thresholds are not judged (NA) when the in-run loadavg mean is >= 6 (PR-5);
//   * case status = worst gating metric: FAIL > ENV_UNMET > WARN > PASS (NA ignored); a P1 case that fails and has a
//     valid waiver becomes WAIVED; P0 is never waived.

export const LOAD_FRAME_MAX = 12.8
export const LOAD_QUALITY_MEAN = 6
export const DISPERSION_MAX = 0.25
const RANK = { FAIL: 5, ENV_UNMET: 4, WARN: 2, PASS: 1, WAIVED: 1, NA: 0 }

export function sorted(a) {
  return Float64Array.from(a).sort()
}
export function quantile(sortedArr, p) {
  const n = sortedArr.length
  if (!n) return null
  return sortedArr[Math.min(n - 1, Math.floor(p * n))]
}
export function mean(a) {
  if (!a.length) return null
  let s = 0
  for (const v of a) s += v
  return s / a.length
}
export function median(values) {
  const v = values.filter((x) => typeof x === 'number' && Number.isFinite(x)).sort((a, b) => a - b)
  if (!v.length) return null
  const m = v.length >> 1
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2
}
export function dispersion(values) {
  const v = values.filter((x) => typeof x === 'number' && Number.isFinite(x))
  if (v.length < 2) return 0
  const med = median(v)
  return (Math.max(...v) - Math.min(...v)) / Math.max(1e-9, Math.abs(med))
}
export function compare(v, op, th) {
  switch (op) {
    case '<': return v < th
    case '<=': return v <= th
    case '==': return Math.abs(v - th) <= 1e-9
    case '>=': return v >= th
    case '>': return v > th
    default: throw new Error(`unknown operator ${op}`)
  }
}

/**
 * @param {{ key: string, gating: boolean, dispersionWatch?: boolean }} m
 * @param {(number|null)[]} runs  per-run values
 * @param {{ max: number[], mean: number[] }} load  per-run loadavg max and mean
 * @param {{ op: string, value: number, kind?: string } | null} th
 */
export function judgeMetric(m, runs, load, th) {
  const med = median(runs)
  const disp = dispersion(runs)
  const out = { key: m.key, runs, median: med, dispersion: disp, threshold: th ? { op: th.op, value: th.value } : null,
    gating: !!m.gating, status: 'PASS', code: undefined }
  if (!th) {
    out.status = 'NA'
    return out
  }
  if (med === null) {
    out.status = m.gating ? 'FAIL' : 'NA'
    out.code = 'PERF-E011'
    return out
  }
  const maxLoad = load.max.length ? Math.max(...load.max) : 0
  const meanLoad = load.mean.length ? mean(load.mean) : 0
  const ok = compare(med, th.op, th.value)
  if (th.kind === 'quality_or_cpu' && meanLoad >= LOAD_QUALITY_MEAN) {
    out.status = 'NA'
    return out
  }
  if (th.kind === 'frame' && maxLoad > LOAD_FRAME_MAX) {
    out.status = 'WARN'
    out.code = 'PERF-E012'
    return out
  }
  if (!ok) {
    out.status = 'FAIL'
    return out
  }
  if (disp > DISPERSION_MAX && m.dispersionWatch) {
    out.status = 'WARN'
    out.code = 'PERF-E013'
  }
  return out
}

export function worst(statuses) {
  let best = null
  for (const s of statuses) {
    if (s === 'NA') continue
    if (best === null || RANK[s] > RANK[best]) best = s
  }
  return best
}

/**
 * @param {{ priority: string, acIds: string[] }} c  CaseDef
 * @param {Array<{ status: string, gating: boolean }>} metrics  judged metrics
 * @param {{ execStatus: string, envUnmet?: boolean }} ex  executor verdict (PASS / FAIL / ENV_UNMET)
 * @param {Array<object>} waivers  valid waivers (validateWaivers)
 */
export function judgeCase(c, metrics, ex, waivers = []) {
  const parts = [ex.execStatus, ...metrics.filter((m) => m.gating).map((m) => m.status)]
  let status = worst(parts) ?? 'NA'
  if (status === 'FAIL' && c.priority !== 'P0') {
    const w = waivers.find((x) => c.acIds.includes(x.ac_id))
    if (w) status = 'WAIVED'
  }
  return status
}

export const WAIVER_FIELDS = ['ac_id', 'priority', 'reason', 'evidence_run', 'owner_module', 'mitigation', 'target_version',
  'approved_by', 'date']

/** 18 §12.3: every field present, priority P1 or P2 (P0 is never waived). Returns { valid, rejected[] }. */
export function validateWaivers(list) {
  const valid = []
  const rejected = []
  for (const w of Array.isArray(list) ? list : []) {
    const missing = WAIVER_FIELDS.filter((f) => w?.[f] === undefined || w?.[f] === null || w?.[f] === '')
    if (missing.length) rejected.push({ waiver: w, reason: `missing ${missing.join(', ')}` })
    else if (w.priority === 'P0') rejected.push({ waiver: w, reason: 'P0 cannot be waived (18 §12.3)' })
    else if (!['P1', 'P2'].includes(w.priority)) rejected.push({ waiver: w, reason: `bad priority ${w.priority}` })
    else valid.push(w)
  }
  return { valid, rejected }
}

// 18 §11.3 regression rules on continuous quantities; returns true when `cur` regressed against `base`
export const REGRESSION = {
  frame_mean_ms: (cur, base) => cur > base * 1.05,
  over50_pct: (cur, base) => cur > base + 1.5,
  ttfp_ms: (cur, base) => cur > base * 1.2 && cur - base > 150,
  b_reversals_per_min: (cur, base) => cur > base + 5,
  cas_switches: (cur, base) => cur > base + 1,
  points_avg: (cur, base) => cur < base * 0.85,
  hole_rate_pct: (cur, base) => cur > base + 3,
  sim_cpu_core: (cur, base) => cur > base * 1.15,
  api_cpu_core: (cur, base) => cur > base * 1.15,
  step_p99_us: (cur, base) => cur > base * 1.2,
  tick_age_p99_ms: (cur, base) => cur > base + 3,
  heap_growth_pct: (cur, base) => cur > base * 1.15,
}
export function isLayerKey(k) {
  return /^layer_[a-zA-Z]+_ms$/.test(k)
}
export function regressed(reportKeyName, cur, base) {
  if (typeof cur !== 'number' || typeof base !== 'number') return null
  if (isLayerKey(reportKeyName)) return cur - base > Math.max(1, 0.25 * base)
  const f = REGRESSION[reportKeyName]
  return f ? f(cur, base) : null
}

/** baseline comparison only when the machine fingerprint matches (18 §11.3 item 2, PERF-E016) */
export function fingerprintMatches(a, b) {
  if (!a || !b) return false
  return ['cpu_model', 'cores', 'chrome', 'flags'].every((k) => JSON.stringify(a[k]) === JSON.stringify(b[k]))
}
