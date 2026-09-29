// M16 harness judging (M16-AC-024; AWR-18 §2.5, §3.1, §11.3, §12.3, §17): nearest-rank quantiles, steady window, metric
// extraction from the three committed synthetic snapshots with known quantiles, four states plus WARN / NA / WAIVED,
// waivers, regression rules and the report mapping.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { cpuCores, extract, steadyIntervals } from '../../perf/harness/analyze.mjs'
import { compare, dispersion, judgeCase, judgeMetric, median, quantile, regressed, sorted, validateWaivers } from '../../perf/harness/judge.mjs'
import { isRegistered, reportKey } from '../../perf/harness/keys.mjs'
import { reportCase, summary } from '../../perf/report/build-report.mjs'

const snaps = [1, 2, 3].map((k) => JSON.parse(readFileSync(join(import.meta.dirname, 'fixtures', `snap-${k}.json`), 'utf8')) as Record<string, unknown>)
const x = (key: string, s: Record<string, unknown>, server?: unknown): number | null =>
  extract({ key, source: key.startsWith('sim_') || key.startsWith('api_') ? 'server' : 'snapshot' }, { snap: s, server })

describe('quantiles and robust statistics', () => {
  it('nearest rank: sorted[min(n - 1, floor(p n))]', () => {
    const s = sorted([5, 1, 4, 2, 3])
    expect(quantile(s, 0.5)).toBe(3)
    expect(quantile(s, 0.95)).toBe(5)
    expect(quantile(s, 0)).toBe(1)
    expect(quantile(sorted([]), 0.5)).toBeNull()
  })
  it('median ignores missing runs; dispersion = (max - min) / median', () => {
    expect(median([3, null, 1, 2])).toBe(2)
    expect(median([1, 2, 3, 4])).toBe(2.5)
    expect(dispersion([40, 50, 60])).toBeCloseTo(0.4)
    expect(compare(1, '<=', 1) && compare(2, '>', 1) && !compare(1, '<', 1) && compare(0.1 + 0.2, '==', 0.3)).toBe(true)
  })
})

describe('extraction from the committed synthetic snapshots (known quantiles)', () => {
  it('steady window excludes the start-up gap at t <= 2 s', () => {
    for (const s of snaps) {
      const iv = steadyIntervals(s)
      expect(iv.length).toBe(1740)
      expect(iv).not.toContain(400)
    }
  })
  it('frame pacing', () => {
    for (const [i, s] of snaps.entries()) {
      expect(x('frame.p50_ms', s)).toBe(33.3)
      expect(x('frame.p95_ms', s)).toBe(45)
      expect(x('frame.p99_ms', s)).toBe(66.7)
      expect(x('max_gap_ms', s)).toBe(i === 1 ? 250 : 120)
      expect(x('over100_pct', s)).toBeCloseTo(((i === 1 ? 11 : 10) / 1740) * 100, 9)
      expect(x('over50_pct', s)).toBeCloseTo(((i === 1 ? 51 : 50) / 1740) * 100, 9)
      expect(x('start_gap_ms', s)).toBe(400)
    }
  })
  it('load, CAS, streaming, latency and worker metrics', () => {
    const s = snaps[0]
    expect(x('ttfp_ms', s)).toBe(750)
    expect(x('b_reversals_per_min', s)).toBeCloseTo(6 / (58 / 60), 9)
    expect(x('dup_download_ratio', s)).toBeCloseTo(1.21, 9)
    expect(x('cpu_cache_peak_mb', s)).toBe(48)
    expect(x('select_p95_ms', s)).toBe(0.3)
    expect(x('fill_rate_pct', s)).toBeCloseTo(97, 9)
    expect(x('worker_decode_p95_ms', s)).toBeCloseTo(1.75, 9)
    expect(x('t_sim_to_pixel_p95_ms', s)).toBe(109)
    expect(x('ours_over50', s)).toBe(0)
    expect(x('sim_cpu_core', s, { proc: { 'sim-core': { cpu_core: 0.31 } } })).toBe(0.31)
    expect(cpuCores({ t: 0, ticks: 0 }, { t: 10, ticks: 300 })).toBe(0.3)
  })
})

describe('judging (four states plus WARN, NA, WAIVED)', () => {
  const th = { op: '<=', value: 50, kind: 'frame' }
  const quiet = { max: [2, 3, 2], mean: [1, 2, 1] }
  it('median of three runs against the threshold', () => {
    const runs = snaps.map((s) => x('frame.p95_ms', s))
    const j = judgeMetric({ key: 'frame.p95_ms', gating: true }, runs, quiet, th)
    expect(j.median).toBe(45)
    expect(j.status).toBe('PASS')
    expect(judgeMetric({ key: 'frame.p95_ms', gating: true }, [60, 70, 40], quiet, th).status).toBe('FAIL')
  })
  it('frame pacing only warns above load 12.8; quality and CPU not judged at mean load >= 6', () => {
    expect(judgeMetric({ key: 'k', gating: true }, [60], { max: [13], mean: [5] }, th)).toMatchObject({ status: 'WARN', code: 'PERF-E012' })
    expect(judgeMetric({ key: 'k', gating: true }, [0.9], { max: [8], mean: [6.2] }, { op: '<=', value: 0.6, kind: 'quality_or_cpu' }).status).toBe('NA')
  })
  it('dispersion above 25 % warns on watched metrics (PERF-E013)', () => {
    expect(judgeMetric({ key: 'over50_pct', gating: true, dispersionWatch: true }, [1, 2, 3], quiet, { op: '<=', value: 5 }))
      .toMatchObject({ status: 'WARN', code: 'PERF-E013' })
    expect(judgeMetric({ key: 'x', gating: true }, [1, 2, 3], quiet, { op: '<=', value: 5 }).status).toBe('PASS')
  })
  it('missing gating metric fails closed; case status is the worst gating state', () => {
    expect(judgeMetric({ key: 'k', gating: true }, [null, null], quiet, th).status).toBe('FAIL')
    expect(judgeCase({ priority: 'P0', acIds: [] }, [{ status: 'WARN', gating: true }, { status: 'NA', gating: true }], { execStatus: 'PASS' })).toBe('WARN')
    expect(judgeCase({ priority: 'P0', acIds: [] }, [{ status: 'FAIL', gating: false }], { execStatus: 'PASS' })).toBe('PASS')
    expect(judgeCase({ priority: 'P0', acIds: [] }, [], { execStatus: 'ENV_UNMET' })).toBe('ENV_UNMET')
  })
  it('waivers: P0 never waived, incomplete rejected, P1 FAIL -> WAIVED', () => {
    const w = { ac_id: 'D1-AC-18', priority: 'P1', reason: 'r', evidence_run: 'e', owner_module: 'M12', mitigation: 'm', target_version: 'V0.2',
      approved_by: 'a', date: '2026-09-29' }
    const v = validateWaivers([w, { ...w, priority: 'P0' }, { ac_id: 'X' }])
    expect(v.valid).toHaveLength(1)
    expect(v.rejected).toHaveLength(2)
    expect(judgeCase({ priority: 'P1', acIds: ['D1-AC-18'] }, [{ status: 'FAIL', gating: true }], { execStatus: 'PASS' }, v.valid)).toBe('WAIVED')
  })
  it('regression rules of 18 §11.3 on continuous quantities', () => {
    expect(regressed('frame_mean_ms', 35, 33)).toBe(true)
    expect(regressed('frame_mean_ms', 34, 33)).toBe(false)
    expect(regressed('ttfp_ms', 900, 700)).toBe(true)
    expect(regressed('ttfp_ms', 820, 700)).toBe(false)
    expect(regressed('layer_drones_ms', 3.2, 2)).toBe(true)
    expect(regressed('frame_p95_ms', 60, 50)).toBeNull()
  })
})

describe('report mapping (18 §11.2)', () => {
  it('dotted CaseDef keys map to registered names; namespaced module keys are accepted', () => {
    expect(reportKey('frame.p95_ms')).toBe('frame_p95_ms')
    expect(reportKey('load.ttfp_ms')).toBe('ttfp_ms')
    expect(reportKey('layer.drones_ms')).toBe('layer_drones_ms')
    expect(reportKey('made.up')).toBeNull()
    expect(isRegistered('gc_pct') && !isRegistered('toast_count')).toBe(true)
  })
  it('fake-source cases are not judged in the report; summary counts P0 and P1', () => {
    const base = { runs: [{ load: { pre: 1, max: 2, mean: 1 } }], errors: [], fingerprint: {} }
    const live = reportCase({ ...base, source: 'live', status: 'PASS', case: { id: 'a', acIds: ['D1-AC-03a'], priority: 'P0', layer: 'core', params: { scene: 'pc' } },
      metrics: [{ key: 'frame.p95_ms', report_key: 'frame_p95_ms', unit: 'ms', runs: [45], median: 45, threshold: { op: '<=', value: 50 }, status: 'PASS' },
        { key: 'toast_count', report_key: null, unit: 'count', runs: [1], median: 1, threshold: null, status: 'NA' }] })
    const fake = reportCase({ ...base, source: 'fake', status: 'PASS', case: { id: 'b', acIds: ['D1-AC-35'], priority: 'P0', layer: 'core', params: {} }, metrics: [] })
    expect(live.metrics.map((m) => m.key)).toEqual(['frame_p95_ms'])
    expect(live.scene).toBe('pc')
    expect(fake.status).toBe('NA')
    const s = summary([live, fake, { ...live, id: 'c', priority: 'P1', status: 'FAIL' }])
    expect(s).toMatchObject({ p0_pass: 1, p0_total: 1, p1_pass: 0, p1_total: 1 })
  })
})
