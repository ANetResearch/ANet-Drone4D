// M13 sensor-simulation cases for the M16 harness (M16-FR-041, CaseDef of M16 §6.7.3; M13 §10; AWR-18 §7). Owner: M13.
// M13-AC-027 has two parts: (1) the sensors stage and m13.* slow tasks inside the fleet ladder n1000 (the core case
// `fleet-ladder` of perf/harness/cases/backend.mjs reports stage_ms_per_s.sensors; the 10 ms/s budget is read from its
// bench-result.json) and (2) the single-call micro benchmarks below (ladder stage p99 <= 200 us, S1 gimbals <= 250 us,
// S3 detection tick <= 1.3 ms, packing <= 0.2 ms per slice, white-noise observation <= 0.3 ms, sensors block <= 1 MB).
// The browser parts of M13-AC-007/012/026/030 run inside the M16 specs (warmup.spec.ts, latency.spec.ts) until M13 ships
// its own perf/m13 specs.
/** @type {import('../harness/types').CaseDef[]} */
export default [
  {
    id: 'm13.stage-bench', kind: 'pytest', cmd: ['tests/sensors/bench_stage.py', '-m', 'perf'], build: 'none',
    backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 3, timeoutS: 600, acIds: ['M13-AC-027', 'D1-AC-07'],
    priority: 'P0', layer: 'core', gates: ['G2d', 'G3', 'G4'], metrics: [], owner: 'M13',
  },
]
