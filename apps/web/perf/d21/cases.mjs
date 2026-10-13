// D2.1 cases of the M16 harness (WP-13b; M16-FR-041 CaseDef, M16 §6.7.3; AWR-04 §15.14 D2-AC-40 to D2-AC-55). Owner: M16 (and M15
// for its two D2.1 UI specs in perf/m15, registered here at its request, d2-WP-12c-to-M16 item 1).
// The live specs start their own sandbox stack (tests/e2e/sbstack.py through perf/d21/live21.ts, with the D2.1 test
// configuration: power and park thresholds, RELEASE from the build under test, a run-time random identity secret), so their
// backend is 'none'. Builds come from the environment: D21_DIST (test build, VITE_AWR_TEST_SWITCHES=1, `node
// perf/m15/helpers/build-ui.mjs` with M15_DIST=D21_DIST) and D21_DEMO_DIST (VITE_AWR_DEMO=public); unset, live21.ensureBuild()
// builds both into .cache/d21/. Timing bounds (reveal degradation, line shown <= 100 ms, main thread while saving, resume
// <= 6 s, field switch <= 300 ms) are asserted with M16_PERF=1 only; functional runs check the behaviour.
// Python cases: the real-stack pytest modules (tests/e2e, tests/chaos) and the D2.1 benchmarks of tools/bench/sandbox.
// Round 2 (2026-10-11): `d21.seats-live` (live viewer seats full: saving, resume behind another viewer, seat_ready, the boot
// line of a third visitor); boot-live measures D2-AC-41 in the page; mobile-live adds the tablet virtual stick (D2-AC-48);
// identity-live adds the shared show's internal control and the demo bundle scan (M15-AC-227); the capability skips of the
// first round (WP-03b) are failures now.
const pw = { kind: 'pw', browser: 'C1', backend: { kind: 'none', world: 'synthcity' }, params: {}, layer: 'core', owner: 'M16',
  metrics: [], runs: 1, gates: ['G2d'], priority: 'P0' }
const py = { browser: 'C1', backend: { kind: 'tool', world: 'synthcity' }, params: {}, layer: 'core', owner: 'M16', metrics: [], runs: 1,
  gates: ['G2d'], priority: 'P0', build: 'none' }

/** @type {import('../harness/types').CaseDef[]} */
export default [
  // ---------------------------------------------------------------- Playwright, real stack
  { ...pw, id: 'd21.boot-live', spec: 'perf/d21/boot-live.spec.ts', build: 'test', timeoutS: 900,
    acIds: ['D2-AC-40', 'D2-AC-41', 'D2-AC-42', 'M15-AC-220', 'M15-AC-221'] },
  { ...pw, id: 'd21.power-live', spec: 'perf/d21/power-live.spec.ts', build: 'test', timeoutS: 1200,
    acIds: ['D2-AC-44', 'D2-AC-45', 'D2-AC-46', 'D2-AC-43', 'M15-AC-222'] },
  { ...pw, id: 'd21.power-boot', spec: 'perf/d21/power-boot.spec.ts', build: 'test', timeoutS: 600, acIds: ['D2-AC-44'] },
  { ...pw, id: 'd21.seats-live', spec: 'perf/d21/seats-live.spec.ts', build: 'test', timeoutS: 900,
    acIds: ['D2-AC-43', 'D2-AC-44', 'D2-AC-41', 'M15-AC-222'] },
  { ...pw, id: 'd21.identity-live', spec: 'perf/d21/identity-live.spec.ts', build: 'test', timeoutS: 1200,
    acIds: ['D2-AC-51', 'D2-AC-43', 'M15-AC-227'] },
  { ...pw, id: 'd21.field-live', spec: 'perf/d21/field-live.spec.ts', build: 'test', timeoutS: 900,
    acIds: ['D2-AC-52', 'D2-AC-53', 'M15-AC-228', 'M15-AC-229'] },
  { ...pw, id: 'd21.mobile-live', spec: 'perf/d21/mobile-live.spec.ts', build: 'test', timeoutS: 3000,
    acIds: ['D2-AC-47', 'D2-AC-48', 'M15-AC-224', 'M15-AC-225'] },
  { ...pw, id: 'd21.gpu-limits', spec: 'perf/d21/gpu-limits-d21.spec.ts', build: 'test', timeoutS: 2400,
    acIds: ['D2-AC-54', 'D2-AC-49'] },
  // ---------------------------------------------------------------- M15 D2.1 UI specs (FakeSource, scripted REST; owner M15)
  { ...pw, id: 'm15.d21-shell', spec: 'perf/m15/d21-shell.spec.ts', build: 'test', timeoutS: 900, owner: 'M15',
    acIds: ['M15-AC-220', 'M15-AC-221', 'M15-AC-222', 'M15-AC-223', 'M15-AC-227', 'M15-AC-228', 'M15-AC-229', 'D2-AC-40', 'D2-AC-41'] },
  { ...pw, id: 'm15.d21-mobile', spec: 'perf/m15/d21-mobile.spec.ts', build: 'test', timeoutS: 1200, owner: 'M15',
    acIds: ['M15-AC-224', 'M15-AC-225', 'M15-AC-226', 'D2-AC-47', 'D2-AC-50'] },
  // ---------------------------------------------------------------- pytest, real stack (needs_data)
  { ...py, id: 'd21.e2e-park', kind: 'pytest', cmd: ['tests/e2e/test_sandbox_park.py'], timeoutS: 1200,
    acIds: ['D2-AC-45', 'M17-AC-027', 'M17-AC-028', 'M17-AC-029'] },
  { ...py, id: 'd21.e2e-capacity', kind: 'pytest', cmd: ['tests/e2e/test_capacity_e2e.py'], timeoutS: 600,
    acIds: ['D2-AC-42', 'D2-AC-43', 'D2-AC-40', 'M17-AC-025', 'M17-AC-026'] },
  { ...py, id: 'd21.e2e-identity', kind: 'pytest', cmd: ['tests/e2e/test_identity_e2e.py'], timeoutS: 900,
    acIds: ['D2-AC-51', 'M17-AC-030', 'M17-AC-031'] },
  { ...py, id: 'd21.e2e-identity-public', kind: 'pytest', cmd: ['tests/e2e/test_identity_public_e2e.py'], timeoutS: 600,
    acIds: ['D2-AC-51', 'M11-AC-217'] },
  { ...py, id: 'd21.e2e-field', kind: 'pytest', cmd: ['tests/e2e/test_field_sampling_e2e.py'], timeoutS: 600,
    acIds: ['D2-AC-52', 'D2-AC-53', 'M19-AC-021', 'M19-AC-022', 'M19-AC-023'] },
  { ...py, id: 'd21.e2e-viewer', kind: 'pytest', cmd: ['tests/e2e/test_viewer_power_e2e.py'], timeoutS: 900,
    acIds: ['D2-AC-43', 'D2-AC-44', 'D2-AC-42'] },
  { ...py, id: 'd21.e2e-bench-tools', kind: 'pytest', cmd: ['tests/e2e/test_sandbox_power_bench.py'], timeoutS: 1800,
    acIds: ['D2-AC-55', 'D2-AC-46'] },
  { ...py, id: 'd21.chaos-park', kind: 'pytest', cmd: ['tests/chaos/test_sandbox_park_chaos.py', '-m', 'chaos'], timeoutS: 900,
    acIds: ['D2-AC-45'], priority: 'P0' },
  // ---------------------------------------------------------------- nginx site file (owner M00, registered by D2-FX-R2-deploy round 2)
  // D2-AC-42 "under the nginx micro-cache 100 concurrent clients reach sandbox-api <= 1/s" (plus five upstream faults) and the
  // nginx side of M17-AC-020 / M11-AC-211, in a one-off Ubuntu 24.04 container with the distribution nginx 1.24 (the emax
  // version); nothing is installed on the host. Evidence: {runDir}/nginx/nginx-check.json. Exit 8 (docker, the ubuntu:24.04
  // image or the apt mirror missing) is a FAIL, never a silent skip.
  { ...py, id: 'd21.nginx-site', kind: 'shell', owner: 'M00', timeoutS: 900,
    cmd: ['bash', 'tools/deploy/emax/nginx-check.sh', '--out', '{runDir}/nginx'], acIds: ['D2-AC-42', 'M17-AC-020', 'M11-AC-211'] },
  // ---------------------------------------------------------------- benchmarks (MS5, cgroup simulation, exclusive lock)
  { ...py, id: 'd21.bench-capacity-d', kind: 'py', timeoutS: 3600, gates: ['G2w', 'G3'],
    cmd: ['python', 'tools/bench/sandbox/capacity.py', '--variant', 'D', '--internal', '--minutes', '30', '--out', '{runDir}/capacity'],
    acIds: ['D2-AC-55'] },
  { ...py, id: 'd21.bench-power', kind: 'py', timeoutS: 1200, priority: 'P1', gates: ['G2w', 'G3'],
    cmd: ['python', 'tools/bench/sandbox/power.py', '--out', '{runDir}/power'],
    acIds: ['D2-AC-46', 'D2-AC-45'] },
]
