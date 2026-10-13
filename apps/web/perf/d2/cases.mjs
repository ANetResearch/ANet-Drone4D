// D2 cases of the M16 harness (WP-13; M16-FR-041 CaseDef, M16 §6.7.3; AWR-04 §13 D2-AC). Owner: M16 (and M15 for its
// sandbox UI spec, which lives in perf/m15 without a registry of its own, d2-WP-12-to-M16 item 1).
// The live specs start their own sandbox stack (tests/e2e/sbstack.py: supervisor `sandbox` profile behind an nginx-like
// preview, perf/d2/live.ts), so their backend is 'none'. Builds come from the environment: D2_DIST (test build,
// VITE_AWR_TEST_SWITCHES=1; `node perf/m15/helpers/build-ui.mjs` with M15_DIST=D2_DIST) and DEMO_DIST (VITE_AWR_DEMO=public).
// Timing bounds (TTFP / reveal <= 5 % worse than D1, visitor path <= 90 s, visible <= 1 s) are asserted only with M16_PERF=1;
// functional runs check the behaviour.
const base = { kind: 'pw', browser: 'C1', backend: { kind: 'none', world: 'synthcity' }, params: {}, layer: 'core', owner: 'M16',
  metrics: [], runs: 1, gates: ['G2d'], priority: 'P0' }

/** @type {import('../harness/types').CaseDef[]} */
export default [
  { ...base, id: 'd2.boot-progress', spec: 'perf/d2/boot-progress.spec.ts', build: 'test', timeoutS: 420, runs: 3,
    acIds: ['D2-AC-01', 'M15-AC-201'], gates: ['G2d', 'G3', 'G4'],
    metrics: [{ key: 'boot_reveal_ms', unit: 'ms', source: 'script', extra: true }, { key: 'boot_ttfp_ms', unit: 'ms', source: 'script', extra: true }] },
  { ...base, id: 'd2.sandbox-live', spec: 'perf/d2/sandbox-live.spec.ts', build: 'test', timeoutS: 1200,
    acIds: ['D2-AC-34', 'D2-AC-22', 'D2-AC-21', 'M15-AC-211'] },
  { ...base, id: 'd2.demo-live', spec: 'perf/d2/demo-live.spec.ts', build: 'production', timeoutS: 900,
    acIds: ['D2-AC-34', 'D2-AC-37', 'M15-AC-204'] },
  { ...base, id: 'd2.gpu-limits', spec: 'perf/d2/gpu-limits-d2.spec.ts', build: 'test', timeoutS: 420,
    acIds: ['D2-AC-30', 'M06-AC-058', 'M18-AC-020'] },
  { ...base, id: 'd2.integration-live', spec: 'perf/d2/integration-live.spec.ts', build: 'test', timeoutS: 2700,
    acIds: ['D2-AC-24', 'D2-AC-16', 'D2-AC-17', 'D2-AC-35'] },
  { ...base, id: 'm15.sandbox-d2', spec: 'perf/m15/sandbox-d2.spec.ts', build: 'test', timeoutS: 420, owner: 'M15',
    acIds: ['M15-AC-201', 'M15-AC-202', 'M15-AC-203', 'M15-AC-204', 'M15-AC-205', 'M15-AC-206', 'M15-AC-207', 'M15-AC-208'] },
]
