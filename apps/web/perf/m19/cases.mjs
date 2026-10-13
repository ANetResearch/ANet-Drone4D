// M19 Playwright cases (written by M16 for M19, d2-WP-06-to-M16 item 1). Test build (M15_DIST), scripted sandbox REST and the
// FakeSource D2 replay (perf/m15/helpers/d2rest.ts); no backend.
const base = { kind: 'pw', browser: 'C1', build: 'test', backend: { kind: 'none', world: 'synthcity' }, params: {}, layer: 'core',
  owner: 'M19', metrics: [], runs: 1, gates: ['G2d'], priority: 'P0' }

/** @type {import('../harness/types').CaseDef[]} */
export default [
  { ...base, id: 'm19.capture-toast', spec: 'perf/m19/capture-toast.spec.ts', timeoutS: 420, acIds: ['M19-AC-013', 'D2-AC-14', 'D2-AC-25'] },
  { ...base, id: 'm19.hud', spec: 'perf/m19/hud.spec.ts', timeoutS: 420, acIds: ['M19-AC-019', 'D2-AC-14'] },
]
