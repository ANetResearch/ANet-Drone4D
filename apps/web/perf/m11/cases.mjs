// M11 realtime-gateway cases for the M16 harness (M16-FR-041, CaseDef of M16 §6.7.3; AWR-18 §7, §8.6, §8.7). Owner: M11.
// The IPC and gateway benchmarks (ipc.state, ipc.cmd, gw-3clients, gw-10clients, gw-30clients) are core cases in
// perf/harness/cases/backend.mjs; this file registers the browser specs of perf/m11. The harness sets M11_PERF=1 and
// AWR_PERF=1 for owner M11 and passes AWR_PERF_BASE / SKELETON_API of the live backend (handshake.spec.ts then uses the
// running api instead of starting a supervisor). fakesource and backpressure serve a test build themselves (staticServer.ts,
// port 4187 + 10 x AWR_PORT_OFFSET), so their backend is 'none'.
const base = { kind: 'pw', build: 'test', browser: 'C1', params: {}, metrics: [], owner: 'M11' }

/** @type {import('../harness/types').CaseDef[]} */
export default [
  {
    ...base, id: 'm11.backpressure', spec: 'perf/m11/backpressure.spec.ts', backend: { kind: 'none', world: 'shenzhen' }, runs: 3,
    timeoutS: 300, acIds: ['M11-AC-015', 'API-AC-008'], priority: 'P0', layer: 'core', gates: ['G2d', 'G3', 'G4'],
  },
  {
    ...base, id: 'm11.fakesource', spec: 'perf/m11/fakesource.spec.ts', backend: { kind: 'none', world: 'shenzhen' }, runs: 1,
    timeoutS: 300, acIds: ['D1-AC-35', 'M11-AC-040', 'PERF-AC-052'], priority: 'P0', layer: 'core', gates: ['G2d', 'G3', 'G4'],
  },
  {
    ...base, id: 'm11.handshake', spec: 'perf/m11/handshake.spec.ts', backend: { kind: 'live', world: 'shenzhen', scenario: 'free-shenzhen' },
    runs: 1, timeoutS: 300, acIds: ['M11-AC-008', 'M11-AC-011', 'M11-AC-033'], priority: 'P0', layer: 'core', gates: ['G2d', 'G3', 'G4'],
  },
]
