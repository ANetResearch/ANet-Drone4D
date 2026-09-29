// pytest executor (M16-FR-042 `pytest`): perf-marked pytest cases and the functional e2e suites scheduled by the gates.
// JUnit XML gives the counts; metric `tests_failed` (source pytest) is 0 when everything passed.
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { PY, runLogged, statusOfExit, substitute } from './common.mjs'

export function parseJunit(xml) {
  const num = (k) => [...xml.matchAll(new RegExp(`<testsuite[^>]*\\b${k}="(\\d+)"`, 'g'))].reduce((a, m) => a + Number(m[1]), 0)
  const tests = num('tests')
  const failures = num('failures') + num('errors')
  const skipped = num('skipped')
  return { tests, failed: failures, skipped, passed: tests - failures - skipped }
}

export async function runPytest(c, o) {
  const junit = join(o.runDir, 'junit.xml')
  const args = ['-m', 'pytest', ...substitute(c.cmd, { runDir: o.runDir, ...o.params }), '-p', 'no:cacheprovider', '-q',
    `--junitxml=${junit}`]
  const env = { ...process.env, ...(o.env ?? {}), AWR_PERF_LOCK_HELD: process.env.AWR_PERF_LOCK_HELD ?? 'ex' }
  const r = await runLogged(PY, args, { env, runDir: o.runDir, name: 'pytest', timeoutS: o.timeoutS })
  const counts = existsSync(junit) ? parseJunit(readFileSync(junit, 'utf8')) : null
  if (r.code === 5) return { execStatus: 'ENV_UNMET', errors: [{ code: 'NO_TESTS', message: 'pytest collected no tests' }], anomaly: false, pytest: counts }
  const st = statusOfExit(r.code === 1 ? 99 : r.code, r.timedOut)
  if (r.code === 1) st.errors = [{ code: 'TESTS', message: `${counts?.failed ?? '?'} failed of ${counts?.tests ?? '?'}` }]
  return { ...st, anomaly: false, pytest: counts ? { ...counts, tests_failed: counts.failed } : null }
}
