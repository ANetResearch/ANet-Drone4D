// M12 timeline, recording and replay cases for the M16 harness (M16-FR-041, CaseDef of M16 §6.7.3; M12 §10.2; AWR-18 §3).
// Owner: M12. Browser specs need a test build (window.__timeline, window.__perf.time): interp, redraw and smoke serve it
// through the M11 static server (backend 'none'); seek-latency and replay20x start their own supervisor with an on-demand
// replay-worker and a synthetic recording (perf/m12/common.ts startReplayBackend, backend 'tool'). The recorder tools write
// awr.bench.rec.v1 (bench-rec.json) and judge their gates by exit code; the harness keeps the protocol (--runs 1, no lock,
// no load wait inside the tool).
const pw = { kind: 'pw', build: 'test', browser: 'C1', params: {}, metrics: [], owner: 'M12' }
const STATIC = { kind: 'none', world: 'shenzhen' }
const TOOL = { kind: 'tool', world: 'shenzhen' }
const rec = (tool, extra) => ['python', `tools/bench/rec/${tool}.py`, ...extra, '--runs', '1', '--out', '{runDir}', '--no-lock', '--no-load-wait']

/** @type {import('../harness/types').CaseDef[]} */
export default [
  {
    ...pw, id: 'm12.smoke', spec: 'perf/m12/smoke.spec.ts', backend: STATIC, runs: 1, timeoutS: 300,
    acIds: ['M12-AC-007', 'M12-AC-008'], priority: 'P0', layer: 'core', gates: ['G2d', 'G3'],
  },
  {
    ...pw, id: 'm12.interp', spec: 'perf/m12/interp.spec.ts', backend: STATIC, runs: 3, timeoutS: 600,
    acIds: ['M12-AC-061', 'M12-AC-007'], priority: 'P0', layer: 'core', gates: ['G2d', 'G3', 'G4'],
  },
  {
    ...pw, id: 'm12.redraw', spec: 'perf/m12/redraw.spec.ts', backend: STATIC, runs: 3, timeoutS: 300,
    acIds: ['M12-AC-062', 'M12-AC-017'], priority: 'P0', layer: 'core', gates: ['G2d', 'G3', 'G4'],
  },
  {
    ...pw, id: 'm12.seek-latency', spec: 'perf/m12/seek-latency.spec.ts', backend: TOOL, runs: 3, timeoutS: 900,
    acIds: ['D1-AC-18', 'M12-AC-063', 'M12-AC-025'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'],
  },
  {
    ...pw, id: 'm12.replay20x', spec: 'perf/m12/replay20x.spec.ts', backend: TOOL, runs: 3, timeoutS: 1200,
    acIds: ['D1-AC-18', 'M12-AC-043', 'M12-AC-065'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'],
  },
  {
    id: 'm12.bench-write', kind: 'py', cmd: rec('bench_write', ['--n', '1000', '--sim-s', '600']), build: 'none', backend: TOOL,
    params: {}, runs: 3, timeoutS: 900, acIds: ['D1-AC-18', 'M12-AC-034'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3'],
    metrics: [], owner: 'M12',
  },
  {
    id: 'm12.bench-seek', kind: 'py', cmd: rec('bench_seek', ['--n', '1000', '--sim-s', '600']), build: 'none', backend: TOOL,
    params: {}, runs: 3, timeoutS: 1500, acIds: ['D1-AC-18', 'M12-AC-042', 'M12-AC-045'], priority: 'P1', layer: 'ext',
    gates: ['G2w', 'G3'], metrics: [], owner: 'M12',
  },
  {
    id: 'm12.bench-replay', kind: 'py', cmd: rec('bench_replay', ['--n', '1000', '--sim-s', '600', '--rate', '20']), build: 'none',
    backend: TOOL, params: {}, runs: 3, timeoutS: 1500, acIds: ['D1-AC-18', 'M12-AC-043', 'M12-AC-065'], priority: 'P1', layer: 'ext',
    gates: ['G2w', 'G3'], metrics: [], owner: 'M12',
  },
]
