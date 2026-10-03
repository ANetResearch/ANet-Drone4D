// Core backend, IPC, chaos, remote and end-to-end pytest cases (M16 §6.7.4, §6.9, §6.10, §6.12; AWR-18 §7, §8.7, §8.8).
// Tools run their own processes (backend kind 'tool'); the harness keeps the protocol (lock, load, three runs) and passes
// --no-lock --no-load-wait so the tools do not repeat it.
import { m } from './_metrics.mjs'

const b = (key, unit, threshold, gating = true, extra = false) => m(key, unit, threshold, gating, { source: 'bench', extra })
const TOOL = { kind: 'tool', world: 'shenzhen' }
const base = { build: 'none', params: {}, layer: 'core', owner: 'M16', metrics: [] }

/** @type {import('../types').CaseDef[]} */
export default [
  {
    ...base, id: 'fleet-ladder', kind: 'py', backend: TOOL, runs: 3, timeoutS: 1500, params: { judge_n: 1000, pin: 'none' },
    cmd: ['python', 'tools/bench/fleet_ladder/run.py', '--n', '10,50,100,200,500,1000', '--dur', '60', '--runs', '1', '--out', '{runDir}',
      '--no-lock', '--no-load-wait'],
    acIds: ['D1-AC-07', 'PERF-AC-030'], priority: 'P0', gates: ['G2d', 'G3', 'G4'], owner: 'M08',
    metrics: [b('rtf', 'ratio', 'rtf'), b('sim_cpu_core', 'core', 'sim_cpu_core'), b('step_p99_us', 'us', 'step_p99_us'),
      b('step_max_us', 'us', 'step_max_us', true, true), b('catchup_saturated', 'count', 'catchup_saturated', true, true)],
  },
  {
    // ACC-4: the tool starts the 3 Chromium clients itself, so it runs under taskset -c 2-6 like gw-3clients (PR-6); with
    // pin 'none' the clients inherited all 8 CPUs and the tool's AffinityGuard stayed inactive (client_affinity cpus 8)
    ...base, id: 'fleet-ladder.concurrent', kind: 'py', backend: TOOL, runs: 3, timeoutS: 900, params: { judge_n: 1000 },
    cmd: ['python', 'tools/bench/fleet_ladder/run.py', '--n', '1000', '--dur', '60', '--runs', '1', '--with-recorder', '--with-checkpoint',
      '--clients', '3', '--with-flight60', '--out', '{runDir}', '--no-lock', '--no-load-wait'],
    acIds: ['D1-AC-28', 'PERF-AC-038'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'], owner: 'M08',
    metrics: [b('step_p99_us', 'us', 'step_p99_us'), b('step_max_us', 'us', 'step_max_us', true, true),
      b('catchup_saturated', 'count', 'catchup_saturated', true, true), b('tick_age_p99_ms', 'ms', 'tick_age_p99_ms')],
  },
  {
    ...base, id: 'sih-parity', kind: 'pytest', backend: { kind: 'none', world: 'shenzhen' }, runs: 1, timeoutS: 1200,
    cmd: ['tests/sim/test_fleet_sih_parity.py', 'tests/sim/test_fleet_robust.py'], acIds: ['D1-AC-12', 'PERF-AC-056'], priority: 'P0',
    gates: ['G2d', 'G3', 'G4'], owner: 'M08', metrics: [m('tests_failed', 'count', 'tests_failed', true, { source: 'pytest', extra: true })],
  },
  {
    ...base, id: 'ipc.state', kind: 'py', backend: TOOL, runs: 1, timeoutS: 600, params: { n: 1000 },
    cmd: ['python', 'tools/bench/ipc/bench_state.py', '--n', '1000', '--runs', '3', '--out', '{runDir}', '--no-lock', '--no-load-wait'],
    acIds: ['PERF-AC-034'], priority: 'P0', gates: ['G2d', 'G3', 'G4'], owner: 'M11',
    metrics: [b('tick_age_p99_ms', 'ms', 'tick_age_p99_ms'), b('rtf', 'ratio', null, false)],
  },
  {
    ...base, id: 'ipc.cmd', kind: 'py', backend: TOOL, runs: 3, timeoutS: 600,
    cmd: ['python', 'tools/bench/ipc/bench_cmd.py', '--secs', '60', '--cps', '50', '--events', '570', '--out', '{runDir}', '--no-lock',
      '--no-load-wait'], acIds: ['D1-AC-10', 'PERF-AC-037'], priority: 'P0', gates: ['G2d', 'G3', 'G4'], owner: 'M11',
    metrics: [b('cmd_rtt_p99_ms', 'ms', 'cmd_rtt_p99_ms'), b('cmd_failures', 'count', 'cmd_failures', true, true),
      b('event_gaps', 'count', 'event_gaps', true, true)],
  },
  {
    ...base, id: 'gw-3clients', kind: 'py', backend: TOOL, runs: 3, timeoutS: 900, params: { n: 1000, clients: 3 },
    cmd: ['python', 'tools/bench/ipc/bench_state.py', '--n', '1000', '--clients', '3', '--with-flight60', '--runs', '1', '--out', '{runDir}',
      '--no-lock', '--no-load-wait'], acIds: ['D1-AC-08', 'PERF-AC-035'], priority: 'P0', gates: ['G2d', 'G3', 'G4'], owner: 'M11',
    metrics: [b('api_cpu_core', 'core', 'api_cpu_core_3c'), b('tick_age_p99_ms', 'ms', 'tick_age_p99_ms')],
  },
  ...[[10, 'P1', ['G2w', 'G3', 'G4'], 3], [30, 'P2', ['G2w'], 1]].map(([n, pri, gates, runs]) => ({
    ...base, id: `gw-${String(n)}clients`, kind: 'py', runs, timeoutS: 600, params: { n: 1000, clients: n },
    backend: { kind: 'live', world: 'shenzhen', scenario: 'ladder-shenzhen', scenarioProfile: 'n1000', waitMark: 'ladder.steady' },
    cmd: ['node', 'tools/bench/ipc/rt_client.mjs', '--base', '{base}', '--clients', String(n), '--dur', '60'],
    acIds: ['PERF-AC-043'], priority: pri, layer: 'ext', gates, owner: 'M11',
    metrics: [m('swarm_hz_min', 'hz', 'swarm_hz_min', true, { source: 'script', extra: true }),
      m('api_cpu_core', 'core', n === 10 ? 'api_cpu_core_10c' : null, n === 10, { source: 'server' })],
  })),
  {
    ...base, id: 'chaos-core', kind: 'pytest', backend: { kind: 'none', world: 'shenzhen' }, runs: 1, timeoutS: 900,
    cmd: ['tests/chaos', '-m', 'chaos and not ext'], acIds: ['D1-AC-11a', 'PERF-AC-044'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
    metrics: [m('tests_failed', 'count', 'tests_failed', true, { source: 'pytest', extra: true })],
  },
  {
    ...base, id: 'chaos', kind: 'pytest', backend: { kind: 'none', world: 'shenzhen' }, runs: 1, timeoutS: 1500,
    cmd: ['tests/chaos', '-m', 'chaos'], acIds: ['D1-AC-11b', 'PERF-AC-046'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'],
    metrics: [m('tests_failed', 'count', 'tests_failed', true, { source: 'pytest', extra: true })],
  },
  {
    ...base, id: 'remote-smoke', kind: 'shell', backend: { kind: 'live', world: 'shenzhen', scenario: 'free-shenzhen' }, runs: 1,
    timeoutS: 300, cmd: ['bash', 'tests/e2e/remote_smoke.sh', '{base}'], acIds: ['D1-AC-33', 'PERF-AC-058'], priority: 'P0',
    gates: ['G3', 'G4'],
  },
  {
    ...base, id: 'e2e.scenarios', kind: 'pytest', backend: { kind: 'none', world: 'shenzhen', env: { AWR_E2E_FULL: '1' } }, runs: 1,
    timeoutS: 5400,
    cmd: ['tests/e2e/test_scenarios.py'], acIds: ['D1-AC-15', 'D1-AC-16', 'D1-AC-17'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
    metrics: [m('tests_failed', 'count', 'tests_failed', true, { source: 'pytest', extra: true })],
  },
  {
    ...base, id: 'e2e.builtin-worlds', kind: 'pytest', backend: { kind: 'none', world: 'shenzhen' }, runs: 1, timeoutS: 600,
    cmd: ['tests/e2e/test_builtin_worlds.py', 'tests/e2e/test_s1_energy.py', 'tests/e2e/test_scenarios_static.py'],
    acIds: ['D1-AC-01', 'D1-AC-15'], priority: 'P0', gates: ['G2d', 'G3', 'G4'],
    metrics: [m('tests_failed', 'count', 'tests_failed', true, { source: 'pytest', extra: true })],
  },
]
