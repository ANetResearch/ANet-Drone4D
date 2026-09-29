// M05 performance cases for the M16 harness (M16-FR-041, CaseDef of M16 §6.7.3; AWR-18 §4.4, §8.6). Owner: M05.
// The specs in this directory serve dist/ and worlds/ themselves (perf/m05/server.ts, backend kind 'none'); the harness
// sets M05_PERF=1 so the thresholds are asserted, runs them under the exclusive performance lock and reads __perf.
// flight60 scene=pc cases are the harness core cases (flight60.<city>.pc, M16 §6.7.4); this file adds the M05-only ones.
const m = (key, unit, threshold, gating = true) => ({ key, unit, source: 'snapshot', extract: key.replace('.', '_'), ...(threshold ? { threshold } : {}), gating })

/** @type {import('../harness/types').CaseDef[]} */
export default [
  ...['shenzhen', 'newyork', 'shanghai', 'suzhou', 'sanfrancisco', 'chicago'].map((city) => ({
    id: `m05.ttfp.${city}`, kind: 'pw', spec: 'perf/m05/ttfp.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: city },
    params: { city, grep: `TTFP ${city}` }, runs: 3, timeoutS: 180, acIds: ['M05-AC-007', 'D1-AC-02', 'PERF-AC-003'],
    priority: ['sanfrancisco', 'chicago'].includes(city) ? 'P1' : 'P0', layer: 'core', gates: ['G2d'], metrics: [m('load.ttfp_ms', 'ms', 'ttfp_ms')], owner: 'M05',
  })),
  {
    id: 'm05.switch', kind: 'pw', spec: 'perf/m05/switch.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 3,
    timeoutS: 240, acIds: ['M05-AC-008', 'D1-AC-02', 'D1-AC-24'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [m('load.switch_ms', 'ms', 'switch_ms')], owner: 'M05',
  },
  {
    id: 'm05.requests', kind: 'pw', spec: 'perf/m05/requests.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1,
    timeoutS: 120, acIds: ['M05-AC-006', 'M05-AC-018', 'M05-AC-032'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [], owner: 'M05',
  },
  {
    id: 'm05.modes', kind: 'pw', spec: 'perf/m05/modes.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1,
    timeoutS: 180, acIds: ['M05-AC-022', 'D1-AC-25'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [], owner: 'M05',
  },
  {
    id: 'm05.stats', kind: 'pw', spec: 'perf/m05/stats.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1,
    timeoutS: 120, acIds: ['M05-AC-031'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [], owner: 'M05',
  },
  {
    id: 'm05.inject', kind: 'pw', spec: 'perf/m05/inject.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: { pcInject: 'fail:0.05' },
    runs: 3, timeoutS: 300, acIds: ['M05-AC-017'], priority: 'P0', layer: 'core', gates: ['G2w'], metrics: [m('pc.failed_nodes', 'count', 'failed_nodes')], owner: 'M05',
  },
  {
    id: 'm05.stale', kind: 'pw', spec: 'perf/m05/stale.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1,
    timeoutS: 120, acIds: ['M05-AC-033'], priority: 'P0', layer: 'core', gates: ['G2w'], metrics: [], owner: 'M05',
  },
  {
    id: 'm05.contextlost', kind: 'pw', spec: 'perf/m05/contextlost.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {},
    runs: 1, timeoutS: 120, acIds: ['M05-AC-026'], priority: 'P0', layer: 'core', gates: ['G2w'], metrics: [], owner: 'M05',
  },
  {
    id: 'm05.converge', kind: 'pw', spec: 'perf/m05/converge.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 3,
    timeoutS: 300, acIds: ['M05-AC-025', 'PERF-AC-007'], priority: 'P1', layer: 'core', gates: ['G2w'], metrics: [m('pc.converge_ms', 'ms', 'converge_ms')], owner: 'M05',
  },
  {
    id: 'm05.isolation', kind: 'pw', spec: 'perf/m05/isolation.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {},
    runs: 1, timeoutS: 120, acIds: ['M05-AC-029'], priority: 'P0', layer: 'core', gates: ['G2w'], metrics: [], owner: 'M05',
  },
  ...['shenzhen', 'newyork', 'shanghai', 'suzhou'].map((city) => ({
    id: `m05.flight60-invariants.${city}`, kind: 'pw', spec: 'perf/m05/flight60-pc.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: city },
    params: { city, grep: `flight60 scene=pc ${city}` }, runs: 1, timeoutS: 300, acIds: ['M05-AC-013', 'M05-AC-014', 'D1-AC-06'], priority: 'P0', layer: 'core',
    gates: ['G2d'], metrics: [m('pc.budget_violations', 'count', 'budget_violations'), m('pc.dup_download_ratio', 'ratio', 'dup_download_ratio')], owner: 'M05',
  })),
  {
    id: 'm05.pick', kind: 'pw', spec: 'perf/m05/pick.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 3,
    timeoutS: 120, acIds: ['M05-AC-027'], priority: 'P1', layer: 'ext', gates: ['G2w'], metrics: [], owner: 'M05',
  },
]
