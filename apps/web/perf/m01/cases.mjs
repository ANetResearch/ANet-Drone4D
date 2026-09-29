// M01 cases for the M16 harness (M16-FR-041, CaseDef of M16 §6.7.3; AWR-18 §4.4). Owner: M01.
// recon.spec.ts runs its own Mock job and backend (backend kind 'tool'); TTFP of the product world is asserted under the
// harness only (D1-AC-02, D1-AC-22). The Mock end-to-end budget (M01-AC-010) is the perf-marked pytest case.
/** @type {import('../harness/types').CaseDef[]} */
export default [
  {
    id: 'm01.recon.web', kind: 'pw', spec: 'perf/m01/recon.spec.ts', build: 'test', browser: 'C1', backend: { kind: 'tool', world: 'shenzhen' },
    params: {}, runs: 1, timeoutS: 420, acIds: ['D1-AC-22', 'M01-AC-008', 'M01-AC-019'], priority: 'P1', layer: 'ext', gates: ['G2d'],
    metrics: [{ key: 'load.ttfp_ms', unit: 'ms', source: 'snapshot', threshold: 'ttfp_ms', gating: true }], owner: 'M01',
  },
  {
    id: 'm01.recon.budget', kind: 'pytest', cmd: ['.venv/bin/python', '-m', 'pytest', '-q', '-m', 'perf', 'tests/reconstruction/test_mock_perf.py'],
    build: 'none', backend: { kind: 'tool', world: 'shenzhen' }, params: {}, runs: 1, timeoutS: 600,
    acIds: ['M01-AC-010', 'M01-NFR-001', 'M01-NFR-002', 'M01-NFR-003'], priority: 'P1', layer: 'ext', gates: ['G3'], metrics: [], owner: 'M01',
  },
]
