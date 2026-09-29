#!/usr/bin/env node
// Gate suites (M16-FR-047; M16 §6.7.6; AWR-18 §12.1, §12.4): G2d (nightly), G2w (weekly additions), G3 (milestone exit,
// --ms <n>), G4 (release). Each case runs in a child `run.mjs --case <id>` that takes the exclusive lock itself
// (per-case locking); exit codes 6, 11 and 14 are recorded as ENV_UNMET, 1 and 2 as FAIL. When the list is done:
// build-report -> render -> check-report. The time budget (G2d 3 h, G2w 5 h, G3/G4 8 h, not counting lock and load
// waits) marks the remaining cases ENV_UNMET ("时长超限").
// Usage: node perf/harness/suite.mjs --gate G2d|G2w|G3|G4 [--ms <n>] [--dry-run] [--list]
import { spawnSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseArgs } from 'node:util'
import { gitInfo, newRunId } from './protocol.mjs'
import { ROOT, RegistryError, loadRegistry } from './registry.mjs'

export const BUDGET_H = { G2d: 3, G2w: 5, G3: 8, G4: 8 }
// 18 §12.4: acceptance ids of each milestone exit (MS6 = every P1 case)
export const MS_AC = {
  1: ['D1-AC-13', 'D1-AC-20', 'D1-AC-35', 'PERF-AC-001', 'PERF-AC-008', 'PERF-AC-009'],
  2: ['D1-AC-01', 'PERF-AC-060'],
  3: ['D1-AC-34'],
  4: ['D1-AC-07', 'D1-AC-08', 'D1-AC-10', 'D1-AC-11a', 'D1-AC-12', 'D1-AC-15', 'D1-AC-33'],
  5: ['D1-AC-02', 'D1-AC-03a', 'D1-AC-03b', 'D1-AC-04', 'D1-AC-06', 'D1-AC-09a', 'D1-AC-14', 'D1-AC-19', 'D1-AC-20', 'D1-AC-24',
    'D1-AC-25', 'D1-AC-26', 'D1-AC-27', 'D1-AC-32', 'PERF-AC-010'],
}

export function gateCases(cases, gate, ms) {
  if (gate === 'G2d' || gate === 'G2w') return cases.filter((c) => c.gates.includes(gate))
  if (gate === 'G3') {
    if (Number(ms) === 6) return cases.filter((c) => c.priority === 'P1' || c.acIds.includes('PERF-AC-070'))
    const acs = new Set(MS_AC[Number(ms)] ?? [])
    return cases.filter((c) => c.gates.includes('G3') && c.acIds.some((a) => acs.has(a)))
  }
  if (gate === 'G4') return cases.filter((c) => c.priority !== 'P2')
  return []
}

function statusOfExit(code) {
  if (code === 0) return null
  if ([6, 11, 14].includes(code)) return 'ENV_UNMET'
  return 'FAIL'
}

async function main() {
  const { values: a } = parseArgs({ options: { gate: { type: 'string' }, ms: { type: 'string' }, 'dry-run': { type: 'boolean' },
    list: { type: 'boolean' } } })
  if (!BUDGET_H[a.gate ?? '']) {
    console.error('usage: suite.mjs --gate G2d|G2w|G3|G4 [--ms n]')
    return 2
  }
  let cases
  try {
    cases = await loadRegistry()
  } catch (e) {
    if (e instanceof RegistryError) {
      console.error(e.message)
      return 2
    }
    throw e
  }
  const list = gateCases(cases, a.gate, a.ms)
  if (a.list || a['dry-run']) {
    for (const c of list) console.log(`${c.id.padEnd(34)} ${c.priority} ${c.layer} runs ${c.runs} ~${Math.round((c.timeoutS * c.runs) / 60)} min`)
    console.log(`${list.length} cases for ${a.gate}${a.ms ? ` MS${a.ms}` : ''}`)
    return 0
  }
  const runId = newRunId()
  const out = join(process.env.AWR_RUNS_DIR ?? join(ROOT, 'runs'), 'perf', runId)
  mkdirSync(out, { recursive: true })
  const manifest = { schema: 'awr.perf.manifest.v1', run_id: runId, gate: a.gate, ms: a.ms ?? null, started_at: new Date().toISOString(),
    git: gitInfo(), cases: list.map((c) => c.id), outcomes: {} }
  writeFileSync(join(out, 'manifest.json'), JSON.stringify(manifest, null, 1))
  const budgetMs = BUDGET_H[a.gate] * 3600_000
  let spent = 0
  const env = { ...process.env }
  delete env.AWR_PERF_LOCK_HELD                    // per-case locking in the child
  for (const c of list) {
    if (spent > budgetMs) {
      manifest.outcomes[c.id] = { status: 'ENV_UNMET', reason: '时长超限' }
      continue
    }
    const t0 = Date.now()
    const r = spawnSync(process.execPath, [join(ROOT, 'apps/web/perf/harness/run.mjs'), '--case', c.id, '--gate', a.gate, '--run-id', runId],
      { stdio: 'inherit', env })
    spent += Date.now() - t0
    const st = statusOfExit(r.status ?? 1)
    manifest.outcomes[c.id] = { exit: r.status, ...(st ? { status: st } : {}) }
    writeFileSync(join(out, 'manifest.json'), JSON.stringify(manifest, null, 1))
  }
  manifest.finished_at = new Date().toISOString()
  writeFileSync(join(out, 'manifest.json'), JSON.stringify(manifest, null, 1))
  const node = process.execPath
  const rep = join(ROOT, 'apps/web/perf/report')
  const b = spawnSync(node, [join(rep, 'build-report.mjs'), '--run', out, '--gate', a.gate], { stdio: 'inherit' })
  if (b.status !== 0) return 1
  spawnSync(node, [join(rep, 'render.mjs'), '--run', out], { stdio: 'inherit' })
  const chk = spawnSync(node, [join(rep, 'check-report.mjs'), '--run', out], { stdio: 'inherit' })
  console.log(`[suite] ${a.gate}: runs/perf/${runId}/report.html${chk.status === 0 ? '' : '（报告无效，见 check-report 输出）'}`)
  return chk.status === 0 ? 0 : 1
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().then((c) => process.exit(c), (e) => {
    console.error(e?.stack ?? e)
    process.exit(1)
  })
}
