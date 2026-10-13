// M18 Playwright cases (written by M16 for M18, d2-WP-05-to-M16 item 2; AWR-03 §4.3: module cases under perf/m<nn>/).
// Both specs start the real sandbox stack themselves (perf/d2/live.ts), test build from D2_DIST.
const base = { kind: 'pw', browser: 'C1', build: 'test', backend: { kind: 'none', world: 'synthcity' }, params: {}, layer: 'core',
  owner: 'M18', metrics: [], runs: 1, gates: ['G2d'], priority: 'P0' }

/** @type {import('../harness/types').CaseDef[]} */
export default [
  { ...base, id: 'm18.targets-panel', spec: 'perf/m18/targets-panel.spec.ts', timeoutS: 900, acIds: ['M18-AC-017', 'D2-AC-11'] },
  { ...base, id: 'm18.manual-keepout', spec: 'perf/m18/manual-keepout.spec.ts', timeoutS: 1200, acIds: ['M18-AC-019', 'D2-AC-21'] },
]
