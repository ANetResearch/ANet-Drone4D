// Playwright executor (M16-FR-042 `pw`): `npx playwright test <spec> --project perf|e2e` under taskset -c 2-6 (PR-6),
// JSON reporter to <runDir>/pw.json, case parameters in AWR_PERF_CASE_PARAMS, snapshot directory in AWR_PERF_RUN_DIR.
// A skipped test counts as a failure of a gating case (a production build silently skipping test-build specs would
// otherwise pass: SK-E2E request item 3). pageerror and GL errors reported by the spec mark the run as an anomaly
// (PR-11: the run is discarded and repeated once).
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { CHROME } from '../protocol.mjs'
import { pinned } from '../browser.mjs'
import { WEB_DIR } from '../registry.mjs'
import { runLogged } from './common.mjs'

function collect(suite, out) {
  for (const s of suite.suites ?? []) collect(s, out)
  for (const sp of suite.specs ?? []) {
    for (const t of sp.tests ?? []) {
      const last = t.results?.[t.results.length - 1]
      out.push({ title: sp.title, status: last?.status ?? t.status, expected: t.expectedStatus,
        errors: (last?.errors ?? []).map((e) => String(e.message ?? e).slice(0, 500)),
        annotations: t.annotations ?? [] })
    }
  }
  return out
}

/** parse the Playwright JSON report: { tests[], passed, failed, skipped } */
export function parseReport(json) {
  const tests = []
  for (const s of json?.suites ?? []) collect(s, tests)
  const n = (st) => tests.filter((t) => t.status === st).length
  return { tests, passed: n('passed'), failed: n('failed') + n('timedOut') + n('interrupted'), skipped: n('skipped') }
}

const ANOMALY = /pageerror|PERF-E003|PERF-E004|GL error|context lost/i

/**
 * @param {import('../types').CaseDef} c
 * @param {{ runDir: string, base?: string|null, params: object, env?: object, owner?: string, timeoutS: number }} o
 */
export async function runPw(c, o) {
  const project = c.spec.startsWith('tests/') ? 'e2e' : 'perf'
  const specArg = c.spec                    // matched against the absolute file path (apps/web/perf/... or tests/e2e/...)
  const args = ['playwright', 'test', specArg, '--project', project, '--workers', '1', '--reporter', 'json',
    ...(o.params.grep ? ['-g', String(o.params.grep)] : [])]
  const mod = /^M(\d\d)$/.exec(c.owner ?? '')
  const env = {
    ...process.env, ...(o.env ?? {}),
    AWR_PERF: '1', AWR_PERF_CASE: c.id, AWR_PERF_CASE_PARAMS: JSON.stringify(o.params), AWR_PERF_RUN_DIR: o.runDir,
    PLAYWRIGHT_JSON_OUTPUT_NAME: join(o.runDir, 'pw.json'), PW_CHROME: CHROME,
    ...(o.base ? { AWR_PERF_BASE: o.base, SKELETON_API: o.base.replace(/^https?:\/\//, '') } : {}),
    ...(mod ? { [`M${mod[1]}_PERF`]: '1' } : {}),
    ...(c.browser === 'C2' ? { AWR_PERF_FLAGS: 'C2' } : {}),
  }
  const npx = join(WEB_DIR, '..', '..', 'node_modules', '.bin', 'playwright')
  const [cmd, a] = pinned(existsSync(npx) ? npx : 'npx', existsSync(npx) ? args.slice(1) : args)
  const r = await runLogged(cmd, a, { cwd: WEB_DIR, env, runDir: o.runDir, name: 'playwright', timeoutS: o.timeoutS })
  const file = join(o.runDir, 'pw.json')
  if (!existsSync(file)) {
    return { execStatus: 'FAIL', errors: [{ code: 'EXEC', message: `no Playwright report (exit ${r.code}${r.timedOut ? ', timeout' : ''})` }], anomaly: false }
  }
  const rep = parseReport(JSON.parse(readFileSync(file, 'utf8')))
  const errors = []
  for (const t of rep.tests.filter((x) => x.status !== 'passed')) {
    errors.push({ code: t.status === 'skipped' ? 'SKIPPED' : 'SPEC', message: `${t.title}: ${t.status} ${t.errors.join(' | ')}`.slice(0, 800) })
  }
  const anomaly = rep.tests.some((t) => t.errors.some((e) => ANOMALY.test(e)))
  const ok = rep.failed === 0 && rep.skipped === 0 && rep.passed > 0 && !r.timedOut
  return { execStatus: ok ? 'PASS' : 'FAIL', errors, anomaly, counts: { passed: rep.passed, failed: rep.failed, skipped: rep.skipped } }
}
