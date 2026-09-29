#!/usr/bin/env node
// Harness CLI (M16-FR-040; M16 §7.1; AWR-18 §3). Performance cases run only through this entry point.
//   node perf/harness/run.mjs --case <id|glob|alias> [--city --scene --n --net --clients] [--source live|fake]
//                             [--runs 1|3] [--gate G2d|G2w|G3|G4|local] [--run-id <id>] [--locked] [--keep-backend]
//   node perf/harness/run.mjs --list [--gate G2d]            list registered cases (no lock)
//   node perf/harness/run.mjs --check                        validate the registry (exit 2 on PERF-E017)
// Exit codes (19 §16.2): 0 judged (see result.json, may be FAIL); 1 tool error; 2 registry or argument error (PERF-E017);
// 6 world or flight60 binding not met (PERF-E009); 11 lock busy or demo runtime active; 14 load or lock wait timeout.
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseArgs } from 'node:util'
import { ensureExclusiveLock, gitInfo, newRunId } from './protocol.mjs'
import { ROOT, RegistryError, loadRegistry, selectCases, skippedDirs } from './registry.mjs'
import { runCase } from './runner.mjs'

export function parse(argv) {
  const { values } = parseArgs({ args: argv, allowPositionals: true, options: {
    case: { type: 'string' }, city: { type: 'string' }, scene: { type: 'string' }, n: { type: 'string' }, net: { type: 'string' },
    profile: { type: 'string' }, clients: { type: 'string' }, source: { type: 'string', default: 'live' }, runs: { type: 'string' },
    gate: { type: 'string', default: 'local' }, 'run-id': { type: 'string' }, locked: { type: 'boolean' }, 'no-lock': { type: 'boolean' },
    'keep-backend': { type: 'boolean' }, list: { type: 'boolean' }, check: { type: 'boolean' }, record: { type: 'boolean' },
    json: { type: 'boolean' }, lenient: { type: 'boolean' },
  } })
  return values
}

/** active demo runtime (M16-NFR-011): a supervisor started with the demo profile refuses chaos and perf runs */
export function demoRuntimeActive() {
  for (const d of readdirSync('/proc')) {
    if (!/^\d+$/.test(d)) continue
    try {
      const cmd = readFileSync(`/proc/${d}/cmdline`, 'utf8').split('\0')
      if (!cmd.some((x) => x === 'awr.runtime.supervisor')) continue
      const i = cmd.indexOf('--profile')
      let prof = i >= 0 ? cmd[i + 1] : null
      if (!prof) prof = /(?:^|\0)AWR_PROFILE=([^\0]*)/.exec(readFileSync(`/proc/${d}/environ`, 'utf8'))?.[1] ?? 'dev'
      if (prof === 'demo') return Number(d)
    } catch {
      // not ours or gone
    }
  }
  return 0
}

async function main() {
  const a = parse(process.argv.slice(2))
  // --check --lenient (make lint): invalid module registries (perf/<module>/cases.mjs) are reported as warnings so that one
  // module's draft does not break everyone's lint; errors in the core registry (perf/harness/cases) stay fatal.
  // AWR_PERF_REGISTRY_SKIP (local debugging only) skips module directories; gates ignore it.
  const skip = a.gate && a.gate !== 'local' ? new Set() : skippedDirs()
  if (skip.size) console.error(`warning: registry directories skipped for this local run: ${[...skip].join(', ')}`)
  let cases
  try {
    cases = await loadRegistry({ skip })
  } catch (e) {
    if (e instanceof RegistryError) {
      const core = e.errors.filter((x) => /\(harness\/cases\//.test(x) || !/\(\w[\w-]*\/cases\.mjs\)|cases\.mjs:/.test(x))
      if (a.check && a.lenient && !core.length) {
        console.error(`warning: ${e.message.replace('PERF-E017', 'PERF-E017 (module registries, not fatal for lint)')}`)
        return 0
      }
      console.error(e.message)
      return 2
    }
    throw e
  }
  if (a.check) {
    console.log(`registry OK: ${cases.length} cases`)
    return 0
  }
  if (a.list) {
    const g = a.gate && a.gate !== 'local' ? a.gate : null
    const sel = cases.filter((c) => !g || c.gates.includes(g))
    if (a.json) console.log(JSON.stringify(sel.map(({ _file, ...c }) => ({ ...c, file: _file })), null, 1))
    else for (const c of sel) console.log(`${c.id.padEnd(34)} ${c.kind.padEnd(6)} ${c.priority} ${c.layer.padEnd(4)} ${c.gates.join(',').padEnd(10)} ${c.acIds.join(',')}`)
    return 0
  }
  if (!a.case) {
    console.error('usage: run.mjs --case <id> (see --list)')
    return 2
  }
  const sel = selectCases(cases, a.case, a)
  if (!sel.length) {
    console.error(`PERF-E017 no case matches ${a.case}`)
    return 2
  }
  if (a.source === 'live' && a.gate !== 'local' && sel.some((c) => c.backend.kind === 'live') && demoRuntimeActive()) {
    console.error('demo profile runtime is active; performance gates refuse to run (M16-NFR-011)；修复：make stop')
    return 11
  }
  const re = ensureExclusiveLock(process.argv, process.env)
  if (re !== null) return re
  const runId = a['run-id'] ?? newRunId()
  const outRoot = join(process.env.AWR_RUNS_DIR ?? join(ROOT, 'runs'), 'perf', runId)
  mkdirSync(outRoot, { recursive: true })
  const man = join(outRoot, 'manifest.json')
  if (!existsSync(man)) {
    writeFileSync(man, JSON.stringify({ schema: 'awr.perf.manifest.v1', run_id: runId, gate: a.gate, started_at: new Date().toISOString(),
      git: gitInfo(), cases: sel.map((c) => c.id) }, null, 1))
  }
  const over = {}
  for (const k of ['city', 'scene', 'n', 'net', 'clients', 'profile']) if (a[k] !== undefined) over[k] = /^\d+$/.test(a[k]) ? Number(a[k]) : a[k]
  if (a.record) over.record = true
  let code = 0
  for (const c of sel) {
    console.log(`[perf] ${c.id} (${c.kind}, ${c.priority}, runs ${a.runs ?? c.runs}) -> runs/perf/${runId}/${c.id}`)
    const r = await runCase(c, { outRoot, gate: a.gate, source: a.source, runs: a.runs ? Number(a.runs) : undefined, params: over,
      keepBackend: a['keep-backend'] })
    const key = r.metrics.filter((m) => m.gating).map((m) => `${m.key}=${m.median === null ? 'NA' : Number(m.median.toFixed(3))}`).join(' ')
    console.log(`[perf] ${c.id}: ${r.status} ${key}${r.errors.length ? ` errors=${r.errors.map((e) => e.code).join(',')}` : ''}`)
    if (r.status === 'ENV_UNMET') {
      const codes = r.errors.map((e) => e.code)
      code = Math.max(code, codes.includes('PERF-E002') ? 14 : codes.includes('PERF-E009') ? 6 : 0)
    }
  }
  return code
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().then((c) => process.exit(c), (e) => {
    console.error(e?.stack ?? e)
    process.exit(1)
  })
}
