// M06 cases for the M16 harness (M16-FR-041, CaseDef of M16 §6.7.3; AWR-18 §4.4, §8.5). Owner: M06.
// The specs serve the build themselves (perf/m06/server.ts: M06_DIST or dist/, worlds/, the ray_hit stand-in) and run
// on FakeSource (backend kind 'none'). The gating D1-AC cases feat-matrix.{B,S,A}, layout and warmup belong to M16
// (perf/feat-matrix.spec.ts, perf/layout.spec.ts, perf/warmup.spec.ts, M16 §6.7.4); the module cases below carry only
// M06 acceptance ids (M16 §14 item 22: same-named module specs do not register the same D1-AC). The harness sets
// M06_PERF=1 so the frame-interval thresholds of warmup and layout are asserted under the exclusive lock.
/** @type {import('../harness/types').CaseDef[]} */
export default [
  {
    id: 'm06.smoke', kind: 'pw', spec: 'perf/m06/smoke.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1,
    timeoutS: 180, acIds: ['M06-AC-001', 'M06-AC-016', 'M06-AC-037'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [], owner: 'M06',
  },
  ...['S', 'B'].map((tier) => ({
    id: `m06.feat-matrix.${tier}`, kind: 'pw', spec: 'perf/m06/feat-matrix.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' },
    params: { tier, grep: `Tier ${tier}` }, runs: 1, timeoutS: 300, acIds: ['M06-AC-001', 'M06-AC-002', 'M06-AC-016'], priority: 'P0', layer: 'core', gates: ['G2d'],
    metrics: [], owner: 'M06',
  })),
  {
    id: 'm06.warmup', kind: 'pw', spec: 'perf/m06/warmup.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1,
    timeoutS: 240, acIds: ['M06-AC-008'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [], owner: 'M06',
  },
  {
    id: 'm06.bench', kind: 'pw', spec: 'perf/m06/bench.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1,
    timeoutS: 420, acIds: ['M06-AC-042'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [], owner: 'M06',
  },
  {
    id: 'm06.layout', kind: 'pw', spec: 'perf/m06/layout.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1,
    timeoutS: 240, acIds: ['M06-AC-048', 'M06-AC-049'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [], owner: 'M06',
  },
  // real-GPU uniform-block limits on SwiftShader (FX-UBO, ADR-086; VERIFY-UBO, ADR-087): emulated 24/12/12/24 (16 384 B blocks)
  // and 36/14/14/28, Tier S and B, test and public demo builds, plus the low-poly instancing read-back on Tier S and B (the
  // spec builds into .cache/gpu-limits unless GPU_LIMITS_TEST_DIST / GPU_LIMITS_DEMO_DIST are given, hence build 'none');
  // functional, no frame-interval threshold
  {
    id: 'm06.gpu-limits', kind: 'pw', spec: 'perf/m06/gpu-limits.spec.ts', build: 'none', browser: 'C1', backend: { kind: 'none', world: 'synthcity' }, params: {},
    runs: 1, timeoutS: 3000, acIds: ['M06-AC-058', 'M06-AC-059'], priority: 'P0', layer: 'core', gates: ['G2d'], metrics: [], owner: 'M06',
  },
]
