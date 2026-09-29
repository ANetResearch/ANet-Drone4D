// One case through the protocol (AWR-18 §3.2 state machine; M16 §6.7.2): WAIT_LOAD -> PREPARE (backend, PR-8 data
// binding) -> RUNNING (executor) -> COLLECTED (snapshot, server.json) x runs, anomaly -> discard and repeat once
// (PR-11) -> ANALYZED (median, dispersion, thresholds, baseline) -> result.json (awr.perf.result.v1).
// Dependencies are injectable (selftest.mjs replaces the backend and the executor, and the load source).
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { extract } from './analyze.mjs'
import { checkAffinity, fetchServerWindow, startBackend, startProcSampler, startProxy, stopBackend, waitMark } from './backend.mjs'
import { FLAGS } from './browser.mjs'
import { runPw } from './exec/pw.mjs'
import { runPy } from './exec/py.mjs'
import { runPytest } from './exec/pytest.mjs'
import { runShell } from './exec/shell.mjs'
import { fingerprintMatches, judgeCase, judgeMetric, regressed, validateWaivers } from './judge.mjs'
import { reportKey } from './keys.mjs'
import { LoadSampler, fingerprint, waitLoad } from './protocol.mjs'
import { readYaml } from './pyjson.mjs'
import { PERF_DIR, ROOT, WEB_DIR } from './registry.mjs'

export const EXECUTORS = { pw: runPw, py: runPy, pytest: runPytest, shell: runShell }
const WORLDS = () => process.env.AWR_WORLDS_DIR ?? join(ROOT, 'worlds')

const sha256File = (p) => createHash('sha256').update(readFileSync(p)).digest('hex')

/** PR-8: world present and flight60 bound to it (coordinate_sha256, bin_sha256); returns error or null (PERF-E009) */
export function dataPrecondition(c, params) {
  const city = params.city ?? c.backend?.world
  if (!city || (c.backend?.kind === 'tool' && !params.city)) return null
  const wj = join(WORLDS(), city, 'world.json')
  if (!existsSync(wj)) return `PERF-E009 world ${city} not built（make worlds）`
  if (!String(c.spec ?? '').includes('flight60') && !String(c.id).startsWith('ladder.front') && !String(c.id).startsWith('flight60')) return null
  const w = JSON.parse(readFileSync(wj, 'utf8'))
  const coord = join(WORLDS(), city, w.coordinate?.href ?? 'coordinate.json')
  for (const base of [join(WEB_DIR, 'dist', 'bench', 'flight60'), join(WEB_DIR, 'public', 'bench', 'flight60')]) {
    const j = join(base, `${city}.json`)
    const b = join(base, `${city}.bin`)
    if (!existsSync(j) || !existsSync(b)) continue
    const f = JSON.parse(readFileSync(j, 'utf8'))
    if (f.coordinate_sha256 !== sha256File(coord)) return `PERF-E009 ${j}: coordinate_sha256 does not match worlds/${city}/coordinate.json（make flight60 && make build）`
    if (f.bin_sha256 !== sha256File(b)) return `PERF-E009 ${b}: bin_sha256 mismatch（make flight60）`
  }
  if (!existsSync(join(WEB_DIR, 'public', 'bench', 'flight60', `${city}.json`))) return `PERF-E009 flight60 for ${city} missing（make flight60）`
  return null
}

/** snapshot validity (18 §2.5 conditions 1-2, PR-7): returns error objects */
export function snapshotErrors(c, snap, validate) {
  const errs = []
  if (!snap) return errs
  if (validate && !validate(snap)) errs.push({ code: 'PERF-E011', message: `snapshot fails awr.perf.v1: ${JSON.stringify(validate.errors?.[0] ?? {}).slice(0, 200)}` })
  const f = snap.forced
  if (f && (f.tier || (f.fixedB && !c.params?.fixedB) || (f.perfInject && !c.params?.perfInject))) {
    errs.push({ code: 'PERF-E005', message: `forced switches in a performance run: ${JSON.stringify(f)}` })
  }
  if (snap.meta && snap.meta.crossOriginIsolated === false) errs.push({ code: 'PERF-E007', message: 'crossOriginIsolated is false' })
  if (snap.meta?.mode && ['production', 'test', 'profiling'].includes(c.build) && snap.meta.mode !== c.build) {
    errs.push({ code: 'PERF-E015', message: `build mode ${snap.meta.mode} != ${c.build}` })
  }
  if ((snap.gpu?.glErrors ?? 0) > 0) errs.push({ code: 'PERF-E004', message: `glErrors ${snap.gpu.glErrors}` })
  return errs
}

let snapshotValidator
async function snapshotValidatorOf() {
  if (snapshotValidator !== undefined) return snapshotValidator
  try {
    const { default: Ajv2020 } = await import('ajv/dist/2020.js')
    const schema = JSON.parse(readFileSync(join(ROOT, 'packages', 'contracts', 'perf', 'perf-snapshot.schema.json'), 'utf8'))
    snapshotValidator = new Ajv2020({ strict: false, allErrors: false }).compile(schema)
  } catch {
    snapshotValidator = null
  }
  return snapshotValidator
}

function readJson(p) {
  return existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')) : null
}

export function loadWaivers() {
  const y = readYaml(join(PERF_DIR, 'waivers.yaml'))
  return validateWaivers(y?.waivers ?? [])
}

/**
 * @param {import('./types').CaseDef} c
 * @param {{ outRoot: string, gate?: string, source?: 'live'|'fake', runs?: number, params?: object, keepBackend?: boolean,
 *   deps?: object, log?: (s: string) => void, waivers?: object[] }} o
 */
export async function runCase(c, o) {
  const log = o.log ?? ((s) => console.log(s))
  const deps = { waitLoad, startBackend, stopBackend, execute: (cc, x) => EXECUTORS[cc.kind](cc, x), LoadSampler,
    fingerprint, snapshotValidator: snapshotValidatorOf, now: () => new Date(), ...(o.deps ?? {}) }
  const params = { ...c.params, ...(o.params ?? {}) }
  const source = o.source === 'fake' || c.backend.kind === 'fake' || params.source === 'fake' ? 'fake' : 'live'
  const backendSpec = source === 'fake' && c.backend.kind === 'live' ? { ...c.backend, kind: 'fake', fakeN: Number(params.n ?? 2) } : c.backend
  const caseDir = join(o.outRoot, c.id)
  mkdirSync(caseDir, { recursive: true })
  const nRuns = Math.min(c.runs, o.runs ?? c.runs)
  const fp = deps.fingerprint(FLAGS[c.browser ?? 'C1'])
  const result = {
    schema: 'awr.perf.result.v1',
    case: { id: c.id, kind: c.kind, params, acIds: c.acIds, priority: c.priority, layer: c.layer, owner: c.owner,
      world: params.city ?? c.backend.world ?? null, scene: params.scene ?? null },
    gate: o.gate ?? 'local', source, runs: [], metrics: [], status: 'NA', errors: [], fingerprint: fp, extra: {},
  }
  const finish = (status, errors = []) => {
    result.status = status
    result.errors.push(...errors)
    writeFileSync(join(caseDir, 'result.json'), JSON.stringify(result, null, 1))
    return result
  }
  const pre = deps.dataPrecondition ? deps.dataPrecondition(c, params) : dataPrecondition(c, params)
  if (pre) return finish('ENV_UNMET', [{ code: 'PERF-E009', message: pre }])
  const validate = await deps.snapshotValidator()
  const arts = []
  for (let i = 1; i <= nRuns; i++) {
    let retried = false
    for (;;) {
      const w = await deps.waitLoad()
      if (!w.ok) return finish('ENV_UNMET', [{ code: 'PERF-E002', message: `loadavg ${w.load.toFixed(2)} > 4 for ${w.waitedS.toFixed(0)} s` }])
      const runDir = join(caseDir, `run-${i}`)
      mkdirSync(runDir, { recursive: true })
      const started = deps.now()
      const sampler = new deps.LoadSampler().start()
      const errors = []
      let h = null
      let ex = { execStatus: 'FAIL', errors: [], anomaly: false }
      let server = null
      let backendBad = false
      try {
        h = await deps.startBackend(backendSpec, { runDir, profile: params.profile ?? 'perf' })
        if (h?.kind === 'live' && (params.profile ?? 'perf') === 'perf') {
          const aff = checkAffinity(h)
          if (aff.length) errors.push(...aff.map((m) => ({ code: 'PERF-E014', message: m })))
        }
        if (h?.kind === 'live' && backendSpec.waitMark) await waitMark(h, backendSpec.waitMark, Number(params.markTimeoutS ?? 150))
        let base = h?.base ?? null
        if (h?.kind === 'live' && backendSpec.netProfile) base = (await startProxy(h, backendSpec.netProfile, 7)).base
        const ps = h?.kind === 'live' ? startProcSampler(h) : null
        ex = await deps.execute(c, { runDir, base, params, timeoutS: c.timeoutS, env: backendSpec.env })
        const proc = ps?.stop() ?? null
        const window = h?.kind === 'live' ? await fetchServerWindow(h, 60) : null
        const growth = Object.values(proc ?? {}).map((x) => (x.rss_mb?.[0] > 0 && x.rss_mb?.[1] > 0 ? 100 * (x.rss_mb[1] / x.rss_mb[0] - 1) : null))
          .filter((x) => x !== null)
        server = { proc, window, rss_growth_pct: growth.length ? Math.max(...growth) : null }
        writeFileSync(join(runDir, 'server.json'), JSON.stringify(server, null, 1))
        if (h?.exited?.() !== null && h?.exited?.() !== undefined) backendBad = true
      } catch (e) {
        backendBad = true
        errors.push({ code: 'PERF-E008', message: String(e?.message ?? e).slice(0, 500) })
      } finally {
        if (!o.keepBackend) await deps.stopBackend(h)
      }
      const load = sampler.stop()
      const snap = readJson(join(runDir, 'snapshot.json'))
      const snapErrs = snapshotErrors(c, snap, validate)
      errors.push(...snapErrs, ...(ex.errors ?? []))
      // PR-11: pageerror, GL errors (PERF-E004) and an unhealthy backend discard the run and repeat it once
      const anomaly = ex.anomaly || backendBad || snapErrs.some((e) => e.code === 'PERF-E004')
      const run = { index: i, dir: runDir.slice(o.outRoot.length + 1), status: anomaly ? 'FAIL' : ex.execStatus, errors,
        load: { pre: w.load, max: load.max, mean: load.mean }, started_at: started.toISOString(),
        duration_s: (deps.now() - started) / 1000, retried }
      if (anomaly && !retried) {
        log(`  ${c.id} run ${i}: anomaly (${errors.map((e) => e.code).join(',')}), discarded and repeated (PR-11)`)
        retried = true
        continue
      }
      result.runs.push(run)
      arts.push({ snap, server, bench: ex.bench ?? null, script: readJson(join(runDir, 'metrics.json')), pytest: ex.pytest ?? null,
        execStatus: anomaly ? 'FAIL' : ex.execStatus, errors })
      break
    }
  }
  // ---- analyze and judge
  const th = (JSON.parse(readFileSync(join(PERF_DIR, 'thresholds.json'), 'utf8'))).entries
  const loads = { max: result.runs.map((r) => r.load.max), mean: result.runs.map((r) => r.load.mean) }
  const gatingCase = source === 'live'
  const baseline = readJson(join(PERF_DIR, 'baselines', `${c.id}.${deviceClassOf(arts)}.json`))
  const baseOk = baseline && fingerprintMatches(baseline.fingerprint, fp)
  for (const m of c.metrics ?? []) {
    const values = arts.map((a) => extract(m, a, params))
    const j = judgeMetric({ ...m, gating: m.gating && gatingCase }, values, loads, m.threshold ? th[m.threshold] : null)
    const rk = m.extra ? null : reportKey(m.key)
    const b = baseOk && rk ? baseline.metrics?.[rk] ?? null : null
    const row = { key: m.key, report_key: rk, unit: m.unit, runs: values, median: j.median, dispersion: j.dispersion,
      threshold: j.threshold, gating: j.gating, status: j.status, code: j.code, baseline: b,
      delta: b !== null && j.median !== null ? j.median - b : null, regression: b !== null ? regressed(rk, j.median, b) : null }
    if (m.extra) result.extra[m.key.replaceAll('.', '_')] = row
    else result.metrics.push(row)
  }
  const execWorst = arts.length ? arts.map((a) => a.execStatus).reduce((a, b) => (rank(b) > rank(a) ? b : a), 'PASS') : 'FAIL'
  const judged = [...result.metrics, ...Object.values(result.extra)]
  const waivers = o.waivers ?? loadWaivers().valid
  const config = arts.flatMap((a) => a.errors).filter((e) => ['PERF-E004', 'PERF-E005', 'PERF-E006', 'PERF-E007', 'PERF-E011', 'PERF-E014',
    'PERF-E015'].includes(e.code))
  let status = judgeCase(c, judged, { execStatus: config.length ? 'FAIL' : execWorst }, waivers)
  if (!gatingCase && status !== 'FAIL') status = 'NA'
  for (const a of arts) for (const e of a.errors) if (!result.errors.some((x) => x.code === e.code && x.message === e.message)) result.errors.push(e)
  if (!gatingCase) result.errors.push({ code: 'FAKE-SOURCE', message: '合成数据，不参与门禁（source=fake）' })
  return finish(status)
}

function rank(s) {
  return { PASS: 1, WARN: 2, ENV_UNMET: 4, FAIL: 5 }[s] ?? 0
}

function deviceClassOf(arts) {
  const d = arts.find((a) => a.snap?.meta?.deviceClass)?.snap?.meta?.deviceClass
  return d ? String(d).toLowerCase() : 'software'
}

