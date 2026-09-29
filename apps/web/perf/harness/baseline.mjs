#!/usr/bin/env node
// Baseline acceptance (M16-FR-045; AWR-18 §11.3): `make perf-baseline-accept CASE=<id> RUN=<runId>` copies the medians of a
// PASS result into perf/baselines/<case>.<device_class>.json together with the machine fingerprint. Only PASS runs are
// accepted; baselines never update automatically (commit message must start with "perf-baseline:").
// Usage: node perf/harness/baseline.mjs accept --case <id> --run <runId>
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseArgs } from 'node:util'
import { PERF_DIR, ROOT } from './registry.mjs'

export function baselineOf(result, deviceClass) {
  if (result.status !== 'PASS') throw new Error(`only PASS results can become a baseline (status ${result.status})`)
  if (result.source === 'fake') throw new Error('source=fake results never become a baseline')
  const metrics = {}
  for (const m of result.metrics ?? []) if (m.report_key && typeof m.median === 'number') metrics[m.report_key] = m.median
  return { schema: 'awr.perf.baseline.v1', case: result.case.id, device_class: deviceClass, accepted_at: new Date().toISOString(),
    fingerprint: result.fingerprint, metrics }
}

function deviceClass(runDir) {
  for (const r of readdirSync(runDir)) {
    const s = join(runDir, r, 'snapshot.json')
    if (existsSync(s)) return String(JSON.parse(readFileSync(s, 'utf8')).meta?.deviceClass ?? 'software').toLowerCase()
  }
  return 'software'
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: { case: { type: 'string' }, run: { type: 'string' } } })
  if (positionals[0] !== 'accept' || !values.case || !values.run) {
    console.error('usage: baseline.mjs accept --case <id> --run <runId>')
    process.exit(2)
  }
  const dir = join(process.env.AWR_RUNS_DIR ?? join(ROOT, 'runs'), 'perf', values.run, values.case)
  const result = JSON.parse(readFileSync(join(dir, 'result.json'), 'utf8'))
  const dc = deviceClass(dir)
  const b = baselineOf(result, dc)
  mkdirSync(join(PERF_DIR, 'baselines'), { recursive: true })
  const out = join(PERF_DIR, 'baselines', `${values.case}.${dc}.json`)
  writeFileSync(out, `${JSON.stringify(b, null, 1)}\n`)
  console.log(`${out}（提交信息以 "perf-baseline:" 开头并写明理由）`)
}
