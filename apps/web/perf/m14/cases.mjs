// M14 performance cases for the M16 harness (CaseDef of M16 §6.7.3; AWR-18 §4.4, §8.6). Owner: M14.
// m14.panel-overhead: AGENTS panel open vs closed during flight60 scene=full (M14-AC-035, NFR-015).
// m14.bench.s3 / m14.bench.stress: tools/bench/agent/run.py (M14-AC-032, AC-033) — process sampling of agent-runtime during
// S3, and the in-process stress run (latency 0, 200 agents, 64 tasks, 1000 delegations per minute, 10 min).
/** @type {import('../harness/types').CaseDef[]} */
export default [
  {
    id: 'm14.panel-overhead', kind: 'pw', spec: 'perf/m14/panel-overhead.spec.ts', build: 'test', browser: 'C1',
    backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1, timeoutS: 1200, acIds: ['M14-AC-035', 'M14-NFR-015'],
    priority: 'P1', layer: 'ext', gates: ['G2d'], metrics: [], owner: 'M14',
  },
  {
    id: 'm14.bench.s3', kind: 'py', cmd: ['python', 'tools/bench/agent/run.py', '--scenario', 's3-newyork-sar', '--dur', '60'],
    backend: { kind: 'supervisor', world: 'newyork', scenario: 's3-newyork-sar', procs: ['sim-core', 'api', 'agent-runtime'] },
    runs: 3, timeoutS: 300, acIds: ['M14-AC-032', 'M14-NFR-001', 'M14-NFR-002'], priority: 'P1', layer: 'ext', gates: ['G2d'],
    metrics: [], owner: 'M14',
  },
  {
    id: 'm14.bench.stress', kind: 'py', cmd: ['python', 'tools/bench/agent/run.py', '--stress', '--dur', '600'],
    backend: { kind: 'none' }, runs: 1, timeoutS: 900, acIds: ['M14-AC-033', 'M14-NFR-008'], priority: 'P2', layer: 'ext',
    gates: ['G2d'], metrics: [], owner: 'M14',
  },
]
