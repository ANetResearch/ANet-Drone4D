// M07 environment cases for the M16 harness (M16-FR-041, CaseDef of M16 §6.7.3; AWR-18 §1.3, §8.5). Owner: M07.
// The pw specs serve a test build themselves (perf/m07/server.ts: static dist, /worlds, a stub /api) and read window.__env,
// so the backend is 'none'; the harness sets M07_PERF=1 (owner M07), which switches env-switch to its timing assertions.
// Functional runs (`npx playwright test perf/m07`) check programs, counters and liveness only.
const base = { kind: 'pw', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, layer: 'core',
  metrics: [], owner: 'M07' }

/** @type {import('../harness/types').CaseDef[]} */
export default [
  {
    ...base, id: 'm07.env-gpu', spec: 'perf/m07/env-gpu.spec.ts', runs: 1, timeoutS: 420,
    acIds: ['D1-AC-13', 'M07-AC-016', 'PERF-AC-049'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
  },
  {
    ...base, id: 'm07.env-switch', spec: 'perf/m07/env-switch.spec.ts', runs: 3, timeoutS: 600,
    acIds: ['D1-AC-19', 'D1-AC-25', 'M07-AC-018', 'M07-AC-019', 'PERF-AC-048'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
  },
  {
    ...base, id: 'm07.env-visual', spec: 'perf/m07/env-visual.spec.ts', runs: 1, timeoutS: 420,
    acIds: ['M07-AC-020', 'M07-AC-021', 'M07-AC-023', 'M07-AC-045'], priority: 'P0', gates: ['G2d', 'G3'],
  },
  {
    // env stage per-tick cost with N = 1000 (box <= 1.6 ms, dryden <= 2.0 ms p99; CPU judged when load < 6) and the query budget
    id: 'm07.stage-bench', kind: 'pytest', cmd: ['tests/environment/test_stage_bench.py', 'tests/environment/test_query.py', '-m', 'perf'],
    build: 'none', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 3, timeoutS: 900, layer: 'core', owner: 'M07',
    acIds: ['M07-AC-008', 'D1-AC-07'], priority: 'P0', gates: ['G2d', 'G3', 'G4'], metrics: [],
  },
  {
    // turbulence-box generation and AWSL streamline generation budgets (asset build and D1-ext streamlines)
    id: 'm07.assets-bench', kind: 'pytest', cmd: ['tests/environment/test_turb_box.py', 'tests/environment/test_streamlines.py', '-m', 'perf'],
    build: 'none', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1, timeoutS: 900, layer: 'ext', owner: 'M07',
    acIds: ['M07-AC-028'], priority: 'P1', gates: ['G2w'], metrics: [],
  },
]
