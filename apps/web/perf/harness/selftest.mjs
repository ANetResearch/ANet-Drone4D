#!/usr/bin/env node
// Harness self-test (M16-FR-048; PERF-AC-001; M16-AC-022, 023, 026). No browser, no backend, no lock on the real file:
//   lock      an exclusive flock held by a child blocks lockFree(); released -> free again (PR-1)
//   load      waitLoad with a load source stuck at 5 gives up after the timeout -> case ENV_UNMET, PERF-E002 (PR-2)
//   retry     an executor anomaly (pageerror) discards the run and repeats it once; twice -> FAIL (PR-11)
//   judge     three synthetic snapshots with known intervals -> medians and PASS; failing threshold -> FAIL;
//             in-run load max > 12.8 -> WARN (PERF-E012); load mean >= 6 on a CPU metric -> NA (PR-4, PR-5)
//   registry  duplicate id, empty acIds, unknown threshold key, unregistered metric key -> PERF-E017; a new
//             perf/<module>/cases.mjs is discovered without touching the harness (M16-NFR-012)
//   waivers   P0 waivers rejected, incomplete waivers rejected, a valid P1 waiver turns FAIL into WAIVED
// Usage: node perf/harness/selftest.mjs [--registry] [--waivers]   (no flag: everything)
import { spawn } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { judgeCase, validateWaivers } from './judge.mjs'
import { lockFree, waitLoad } from './protocol.mjs'
import { loadRegistry, loadThresholds, registryFiles, validateCases } from './registry.mjs'
import { runCase } from './runner.mjs'

let failed = 0
function check(name, cond, detail = '') {
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${name}${cond ? '' : ` ${detail}`}`)
  if (!cond) failed++
}

const TMP = mkdtempSync(join(tmpdir(), 'awr-perf-selftest-'))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ------------------------------------------------------------ fake dependencies
function snapshot(intervals) {
  const t = intervals.map((_, i) => 2 + (58 * (i + 1)) / intervals.length)
  return { schema: 'awr.perf.v1', meta: { mode: 'production', tier: 'S', deviceClass: 'software', targetMs: 33.3, crossOriginIsolated: true },
    forced: null, frame: { interval: intervals, t, count: intervals.length }, load: { ttfp: 800, revealAt: 1 } }
}
function deps({ runs, load = { max: 2, mean: 2 }, loadOk = true }) {
  let k = 0
  return {
    waitLoad: async () => (loadOk ? { ok: true, load: 1, waitedS: 0 } : waitLoad({ read: () => 5, timeoutS: 30, pollS: 10, sleepMs: async () => {},
      now: (() => { let t = 0; return () => (t += 10_000) })() })),
    startBackend: async () => ({ kind: 'none', base: null, exited: () => null }),
    stopBackend: async () => ({}),
    snapshotValidator: async () => null,
    dataPrecondition: () => null,
    fingerprint: () => ({ cpu_model: 'test', cores: 8, chrome: 'test', flags: [] }),
    LoadSampler: class { start() { return this } stop() { return load } },
    execute: async (_c, o) => {
      const r = runs[Math.min(k++, runs.length - 1)]
      if (r.snap) writeFileSync(join(o.runDir, 'snapshot.json'), JSON.stringify(r.snap))
      return { execStatus: r.status ?? 'PASS', errors: r.errors ?? [], anomaly: !!r.anomaly }
    },
  }
}
const CASE = { id: 'selftest.flight', kind: 'pw', spec: 'perf/flight60.spec.ts', build: 'production', backend: { kind: 'none', world: 'shenzhen' },
  params: {}, runs: 3, timeoutS: 60, acIds: ['PERF-AC-001'], priority: 'P0', layer: 'core', gates: ['G2d'], owner: 'M16',
  metrics: [{ key: 'frame.p95_ms', unit: 'ms', source: 'snapshot', threshold: 'frame_p95_pc', gating: true },
    { key: 'over50_pct', unit: 'pct', source: 'snapshot', threshold: 'over50_pc', gating: true, dispersionWatch: true }] }

async function lockTest() {
  const f = join(TMP, 'perf.lock')
  const child = spawn('flock', ['-x', f, 'sleep', '2'])
  await sleep(300)
  check('lock: busy while another holder has it (PR-1)', lockFree(f) === false)
  await new Promise((r) => child.on('exit', r))
  check('lock: free after release', lockFree(f) === true)
}

async function loadTest() {
  const w = await waitLoad({ read: () => 5, timeoutS: 30, pollS: 10, sleepMs: async () => {}, now: (() => { let t = 0; return () => (t += 10_000) })() })
  check('load: gives up after the timeout when loadavg stays > 4 (PR-2)', w.ok === false)
  const r = await runCase(CASE, { outRoot: join(TMP, 'load'), deps: deps({ runs: [{}], loadOk: false }), log: () => {}, waivers: [] })
  check('load: case ENV_UNMET with PERF-E002', r.status === 'ENV_UNMET' && r.errors.some((e) => e.code === 'PERF-E002'), JSON.stringify(r.errors))
}

async function retryTest() {
  const good = snapshot(Array(1800).fill(33.3))
  const r1 = await runCase({ ...CASE, runs: 1 }, { outRoot: join(TMP, 'retry1'), deps: deps({ runs: [{ anomaly: true, errors: [{ code: 'SPEC', message: 'pageerror' }] }, { snap: good }] }), log: () => {}, waivers: [] })
  check('retry: one anomaly -> repeated once and judged (PR-11)', r1.runs.length === 1 && r1.runs[0].retried === true && r1.status === 'PASS', JSON.stringify(r1.runs))
  const r2 = await runCase({ ...CASE, runs: 1 }, { outRoot: join(TMP, 'retry2'), deps: deps({ runs: [{ anomaly: true }, { anomaly: true }] }), log: () => {}, waivers: [] })
  check('retry: anomaly again after the repeat -> FAIL', r2.status === 'FAIL')
}

async function judgeTest() {
  const a = Array(1800).fill(33.3)
  const s1 = snapshot([...a.slice(0, 1710), ...Array(90).fill(45)])          // p95 = 45, over50 = 0
  const s2 = snapshot([...a.slice(0, 1700), ...Array(100).fill(48)])
  const s3 = snapshot([...a.slice(0, 1690), ...Array(110).fill(49)])
  const r = await runCase(CASE, { outRoot: join(TMP, 'judge'), deps: deps({ runs: [{ snap: s1 }, { snap: s2 }, { snap: s3 }] }), log: () => {}, waivers: [] })
  const p95 = r.metrics.find((m) => m.key === 'frame.p95_ms')
  check('judge: three runs, median of the p95s', r.runs.length === 3 && p95.median === 48 && p95.runs.join() === '45,48,49', JSON.stringify(p95))
  check('judge: PASS within thresholds', r.status === 'PASS', r.status)
  const bad = snapshot([...a.slice(0, 1600), ...Array(200).fill(60)])        // p95 = 60 > 50, over50 11 %
  const rf = await runCase(CASE, { outRoot: join(TMP, 'judgef'), deps: deps({ runs: [{ snap: bad }] }), log: () => {}, waivers: [] })
  check('judge: FAIL above the threshold', rf.status === 'FAIL', rf.status)
  const rw = await runCase(CASE, { outRoot: join(TMP, 'judgew'), deps: deps({ runs: [{ snap: bad }], load: { max: 13, mean: 5 } }), log: () => {}, waivers: [] })
  check('judge: frame pacing only warns when load max > 12.8 (PERF-E012)', rw.status === 'WARN' && rw.metrics.every((m) => m.code === 'PERF-E012'), JSON.stringify(rw.metrics.map((m) => m.status)))
  const cpuCase = { ...CASE, id: 'selftest.cpu', metrics: [{ key: 'sim_cpu_core', unit: 'core', source: 'script', threshold: 'sim_cpu_core', gating: true }] }
  const dCpu = deps({ runs: [{ snap: s1 }], load: { max: 7, mean: 6.5 } })
  const exe = dCpu.execute
  dCpu.execute = async (c, o) => {
    writeFileSync(join(o.runDir, 'metrics.json'), JSON.stringify({ sim_cpu_core: 0.9 }))
    return exe(c, o)
  }
  const rn = await runCase(cpuCase, { outRoot: join(TMP, 'judgen'), deps: dCpu, log: () => {}, waivers: [] })
  check('judge: CPU threshold not judged when load mean >= 6 (NA)', rn.metrics[0].status === 'NA' && rn.status === 'PASS', JSON.stringify(rn.metrics[0]))
}

async function registryTest() {
  const th = loadThresholds()
  const base = { kind: 'shell', cmd: ['true'], build: 'none', backend: { kind: 'none', world: 'shenzhen' }, params: {}, runs: 1, timeoutS: 5,
    priority: 'P0', layer: 'core', gates: ['G2d'], owner: 'M16', metrics: [] }
  const errs = validateCases([
    { ...base, id: 'a', acIds: ['PERF-AC-001'] }, { ...base, id: 'a', acIds: ['PERF-AC-001'] }, { ...base, id: 'b', acIds: [] },
    { ...base, id: 'c', acIds: ['X'], metrics: [{ key: 'frame.p95_ms', unit: 'ms', source: 'snapshot', threshold: 'no_such', gating: true }] },
    { ...base, id: 'd', acIds: ['X'], metrics: [{ key: 'made.up_metric', unit: 'ms', source: 'snapshot', gating: false }] },
    { ...base, id: 'e', acIds: ['X'], kind: 'pw', spec: 'perf/none.spec.ts' },
  ], th)
  for (const [what, re] of [['duplicate id', /duplicate id/], ['empty acIds', /acIds is empty/], ['unknown threshold', /threshold no_such/],
    ['unregistered key', /not an 18 §11.2 registered name/], ['missing spec', /spec not found/]]) {
    check(`registry: rejects ${what} (PERF-E017)`, errs.some((e) => re.test(e)), errs.join(' | '))
  }
  // auto-discovery: a module directory with cases.mjs is found without changing the harness
  const perf = join(TMP, 'perf')
  mkdirSync(join(perf, 'harness', 'cases'), { recursive: true })
  mkdirSync(join(perf, 'm99'), { recursive: true })
  writeFileSync(join(perf, 'thresholds.json'), JSON.stringify(th))
  writeFileSync(join(perf, 'm99', 'cases.mjs'), `export default [${JSON.stringify({ ...base, id: 'm99.x', acIds: ['M99-AC-001'] })}]\n`)
  check('registry: perf/<module>/cases.mjs discovered', registryFiles(perf).some((f) => f.endsWith('m99/cases.mjs')))
  const cases = await loadRegistry({ perfDir: perf, thresholds: th })
  check('registry: module case loaded', cases.some((c) => c.id === 'm99.x'))
  // the core registry must be valid; module registries (perf/<module>/cases.mjs) are only reported here (lint warns, the
  // harness refuses to start until the owning module fixes its file)
  const modules = new Set(registryFiles().map((f) => f.split('/').slice(-2)[0]).filter((d) => d !== 'cases'))
  const core = await loadRegistry({ skip: modules }).then((c) => c, (e) => e)
  check('registry: the core registry (perf/harness/cases) is valid', Array.isArray(core), core?.message ?? '')
  const full = await loadRegistry().then((c) => c, (e) => e)
  if (!Array.isArray(full)) console.log(`warn module registries: ${full.message.split('\n').slice(1).join(' | ')}`)
}

function waiverTest() {
  const full = { ac_id: 'D1-AC-18', priority: 'P1', reason: 'r', evidence_run: 'runs/perf/p1', owner_module: 'M12', mitigation: 'm',
    target_version: 'V0.2', approved_by: 'x', date: '2026-09-29' }
  const v = validateWaivers([full, { ...full, priority: 'P0', ac_id: 'D1-AC-15' }, { ac_id: 'D1-AC-17', priority: 'P1' }])
  check('waivers: valid P1 accepted', v.valid.length === 1 && v.valid[0].ac_id === 'D1-AC-18')
  check('waivers: P0 rejected', v.rejected.some((r) => /P0 cannot be waived/.test(r.reason)))
  check('waivers: incomplete rejected', v.rejected.some((r) => /missing/.test(r.reason)))
  const f = [{ status: 'FAIL', gating: true }]
  check('waivers: P1 FAIL with a waiver -> WAIVED', judgeCase({ priority: 'P1', acIds: ['D1-AC-18'] }, f, { execStatus: 'PASS' }, v.valid) === 'WAIVED')
  check('waivers: P0 FAIL stays FAIL', judgeCase({ priority: 'P0', acIds: ['D1-AC-18'] }, f, { execStatus: 'PASS' }, v.valid) === 'FAIL')
}

async function main() {
  const args = process.argv.slice(2)
  const only = args.filter((a) => a.startsWith('--')).map((a) => a.slice(2))
  const all = !only.length
  try {
    if (all) await lockTest()
    if (all) await loadTest()
    if (all) await retryTest()
    if (all) await judgeTest()
    if (all || only.includes('registry')) await registryTest()
    if (all || only.includes('waivers')) waiverTest()
  } finally {
    rmSync(TMP, { recursive: true, force: true })
  }
  console.log(failed ? `selftest: ${failed} failed` : 'selftest: OK')
  return failed ? 1 : 0
}

main().then((c) => process.exit(c), (e) => {
  console.error(e?.stack ?? e)
  process.exit(1)
})
