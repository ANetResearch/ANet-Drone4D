// Core interaction, UI budget, storm, weak-network and design-system cases (M16 §6.7.4; AWR-18 §6.2, §8.5, §8.7(3)(4), §8.8).
import { frameFull, m, server } from './_metrics.mjs'

const S1 = { kind: 'live', world: 'shenzhen', scenario: 's1-shenzhen-facade', scenarioProfile: 'perf' }
const FREE = { kind: 'live', world: 'shenzhen', scenario: 'free-shenzhen' }
const LADDER = (n) => ({ kind: 'live', world: 'shenzhen', scenario: 'ladder-shenzhen', scenarioProfile: `n${n}`, waitMark: 'ladder.steady' })
const STATIC = { kind: 'none', world: 'shenzhen' }
const pw = (id, spec, o) => ({ id, kind: 'pw', spec, build: 'production', browser: 'C1', runs: 1, timeoutS: 300, params: {},
  layer: 'core', owner: 'M16', metrics: [], ...o })

/** @type {import('../types').CaseDef[]} */
export default [
  pw('skeleton', 'perf/skeleton.spec.ts', {
    build: 'test', backend: { kind: 'live', world: 'shenzhen', env: { AWR_SCENARIO_LOAD: '0' } }, params: { grep: 'full chain' },
    acIds: ['D1-AC-34', 'PERF-AC-051'], priority: 'P0', gates: ['G2d', 'G3', 'G4'], timeoutS: 240,
  }),
  pw('latency', 'perf/latency.spec.ts', {
    backend: S1, params: { city: 'shenzhen' }, runs: 3, acIds: ['D1-AC-26', 'PERF-AC-040'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
    metrics: [m('t_sim_to_pixel_p95_ms', 'ms', 't_sim_to_pixel_p95_ms'), m('cmd_to_visible_p95_ms', 'ms', null),
      m('cmd_to_visible_excess_ms', 'ms', 'cmd_to_visible_excess_ms', true, { extra: true }), m('hold_pct', 'pct', 'hold_pct', true, { extra: true }),
      // FX2-R3: the remaining D1-AC-26 sub-items (focus-set switch, x10 HOLD, selected channel against rAF, credit skips)
      m('focus_jump_max_m', 'm', 'focus_jump_m', true, { source: 'script', extra: true }),
      m('hold_pct_x10', 'pct', 'hold_pct', true, { source: 'script', extra: true }),
      m('selected_hz_over_raf', 'ratio', 'sel_hz_over_raf', true, { source: 'script', extra: true }),
      m('credit_skips_sel_pct', 'pct', 'credit_skips_sel_pct', true, { source: 'script', extra: true })],
  }),
  pw('storm.rtl', 'perf/storm.spec.ts', {
    backend: LADDER(1000), params: { city: 'shenzhen', n: 1000, storm: 'rtl', grep: 'RTL all' }, runs: 3, timeoutS: 420,
    acIds: ['D1-AC-27', 'PERF-AC-041', 'PERF-AC-026'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
    metrics: [m('ours_over50', 'count', 'ours_over50'), m('toast_count', 'count', 'toast_count', true, { source: 'script', extra: true }),
      m('rail_overflow_rows', 'count', 'rail_overflow_rows', true, { source: 'script', extra: true }),
      m('step_max_us', 'us', 'step_max_us', true, { source: 'server', extra: true }), ...server()],
  }),
  pw('storm.linkdrop', 'perf/storm.spec.ts', {
    backend: LADDER(1000), params: { city: 'shenzhen', n: 1000, storm: 'linkdrop', grep: 'link_drop' }, runs: 3, timeoutS: 420,
    acIds: ['D1-AC-27', 'PERF-AC-041'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'],
    metrics: [m('ours_over50', 'count', 'ours_over50'), m('toast_count', 'count', 'toast_count', true, { source: 'script', extra: true }),
      m('rail_overflow_rows', 'count', 'rail_overflow_rows', true, { source: 'script', extra: true })],
  }),
  pw('storm.flood', 'perf/storm.spec.ts', {
    build: 'test', backend: FREE, params: { city: 'shenzhen', storm: 'flood', grep: 'event flood' }, runs: 3,
    acIds: ['PERF-AC-026'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'],
    metrics: [m('ours_over50', 'count', 'ours_over50'), m('toast_count', 'count', 'toast_count', true, { source: 'script', extra: true })],
  }),
  pw('layout', 'perf/layout.spec.ts', {
    backend: S1, params: { city: 'shenzhen' }, runs: 3, acIds: ['D1-AC-24', 'PERF-AC-027'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
    metrics: [m('rt_allocs_delta', 'count', 'rt_allocs_delta', true, { source: 'script', extra: true }),
      m('over50_delta_pct', 'pct', 'over50_delta_pct', true, { source: 'script', extra: true })],
  }),
  pw('warmup', 'perf/warmup.spec.ts', {
    backend: S1, params: { city: 'shenzhen' }, runs: 3, acIds: ['D1-AC-25', 'PERF-AC-028'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
    metrics: [m('programs_delta', 'count', 'programs_delta', true, { source: 'script', extra: true }),
      m('after_op_max_gap_ms', 'ms', 'after_op_max_gap_ms', true, { source: 'script', extra: true })],
  }),
  pw('ui-overhead', 'perf/ui-overhead.spec.ts', {
    backend: S1, params: { city: 'shenzhen', blocks: 3 }, runs: 1, timeoutS: 900, acIds: ['D1-AC-23', 'PERF-AC-047'], priority: 'P1',
    layer: 'ext', gates: ['G2w', 'G3', 'G4'],
    metrics: [m('p50_delta_ms', 'ms', 'p50_delta_ms', true, { source: 'script', extra: true }),
      m('over50_delta_pct', 'pct', 'over50_delta_pct', true, { source: 'script', extra: true })],
  }),
  pw('ui-commit', 'perf/ui-commit.spec.ts', {
    build: 'profiling', backend: S1, params: { city: 'shenzhen', scene: 'full' }, runs: 3, acIds: ['PERF-AC-020'], priority: 'P1',
    layer: 'ext', gates: ['G2w', 'G3', 'G4'],
    metrics: [m('react_commit_p95_ms', 'ms', 'react_commit_p95_ms', true, { extra: true }),
      m('react_commits_per_s', 'hz', 'react_commits_per_s', true, { source: 'script', extra: true })],
  }),
  pw('gc', 'perf/gc.spec.ts', {
    backend: S1, params: { city: 'shenzhen', scene: 'full' }, runs: 3, acIds: ['D1-AC-30', 'PERF-AC-029'], priority: 'P1', layer: 'ext',
    gates: ['G2w', 'G3', 'G4'], metrics: [m('gc_pct', 'pct', 'gc_pct', true, { source: 'script' })],
  }),
  ...['B', 'S', 'A'].map((tier) => pw(`feat-matrix.${tier}`, 'perf/m06/feat-matrix.spec.ts', {
    build: 'test', browser: tier === 'A' ? 'C2' : 'C1', backend: STATIC, params: { tier, grep: `Tier ${tier}` },
    acIds: ['D1-AC-14', 'PERF-AC-014'], priority: tier === 'A' ? 'P1' : 'P0', layer: tier === 'A' ? 'ext' : 'core',
    gates: tier === 'A' ? ['G2w', 'G3', 'G4'] : ['G2d', 'G3', 'G4'],
  })),
  pw('soak', 'perf/soak.spec.ts', {
    backend: { kind: 'live', world: 'shenzhen', scenario: 'soak-shenzhen' }, params: { city: 'shenzhen', minutes: 30 }, runs: 1, timeoutS: 2400,
    acIds: ['D1-AC-29', 'PERF-AC-045'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'],
    metrics: [...frameFull(), m('heap_growth_pct', 'pct', 'heap_growth_pct', true, { source: 'script' }),
      m('rss_growth_pct', 'pct', 'rss_growth_pct', true, { source: 'script', extra: true }),
      m('unexpected_reconnects', 'count', 'unexpected_reconnects', true, { source: 'script', extra: true })],
  }),
  ...['W0', 'W1', 'W2', 'W3'].map((w) => pw(`net.${w}`, 'perf/net.spec.ts', {
    backend: { ...LADDER(200), netProfile: w }, params: { city: 'shenzhen', scene: 'full', n: 200, net: w }, runs: 3, timeoutS: 420,
    acIds: ['PERF-AC-042'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'],
    metrics: [m('display_drift_ms', 'ms', 'display_drift_ms', true, { extra: true }), m('swarm_hz', 'hz', 'swarm_hz_min', true, { extra: true }),
      m('ttfp_ms', 'ms', 'ttfp_ms', w !== 'W3'), m('over50_pct', 'pct', null)],
  })),
  pw('bench-upload', 'perf/bench.spec.ts', {
    build: 'test', backend: FREE, params: { tier: 'B' }, acIds: ['PERF-AC-066'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'],
  }),
  pw('e2e.interaction', 'tests/e2e/interaction.spec.ts', {
    build: 'test', backend: FREE, params: { city: 'shenzhen' }, acIds: ['D1-AC-32', 'PERF-AC-025'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
    metrics: [m('switch_ms', 'ms', 'switch_ms', true, { source: 'script' })],
  }),
  pw('e2e.honesty', 'tests/e2e/honesty.spec.ts', {
    backend: FREE, acIds: ['PRD-AC-005'], priority: 'P1', layer: 'ext', gates: ['G2d', 'G3', 'G4'],
  }),
  ...['motion', 'brand', 'sanitize'].map((s) => pw(`e2e.${s}`, `tests/e2e/${s}.spec.ts`, {
    build: 'test', backend: STATIC, acIds: ['D1-AC-20', 'PERF-AC-024'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
  })),
  pw('e2e.a11y', 'tests/e2e/a11y.spec.ts', {
    build: 'test', backend: STATIC, acIds: ['D1-AC-21'], priority: 'P1', layer: 'ext', gates: ['G2d', 'G3', 'G4'],
  }),
  pw('e2e.timeline', 'tests/e2e/timeline.spec.ts', {
    build: 'test', backend: { kind: 'live', world: 'shenzhen', scenario: 'free-shenzhen' }, timeoutS: 420,
    acIds: ['D1-AC-18'], priority: 'P1', layer: 'ext', gates: ['G2d', 'G3', 'G4'],
  }),
  pw('demo.rehearsal', 'tests/e2e/demo_rehearsal.spec.ts', {
    build: 'test', backend: { kind: 'live', world: 'shenzhen', scenario: 's1-shenzhen-facade', scenarioProfile: 'demo' },
    params: { city: 'shenzhen' }, timeoutS: 900, acIds: ['M16-AC-018'], priority: 'P1', layer: 'ext', gates: ['G3', 'G4'],
  }),
]
