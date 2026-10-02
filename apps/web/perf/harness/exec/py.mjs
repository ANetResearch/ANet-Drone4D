// Tool executor (M16-FR-042 `py`): Python or Node benchmark tools that write awr.bench.result.v1. Tools that start
// their own browsers or clients (gw-3clients, fleet-ladder.concurrent, rt_client) are wrapped in taskset -c 2-6 as a
// whole (PR-12: the clients belong to the case); `params.pin = 'none'` leaves the tool unpinned (it pins its children).
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pinned } from '../browser.mjs'
import { findNewest, resolveCmd, runLogged, statusOfExit, substitute } from './common.mjs'

/** Python's json.dump writes NaN and Infinity for failed measurements (ACC-1: fleet_ladder N = 500/1000 whose sim-core never
 *  came up); they become null so the record is judged as missing instead of crashing the run as an anomaly */
export function parseBench(text) {
  return JSON.parse(text.replace(/(:\s*|\[\s*|,\s*)(-?Infinity|NaN)(?=\s*[,\]}])/g, '$1null'))
}

export async function runPy(c, o) {
  const [cmd0, args0] = resolveCmd(substitute(c.cmd, { runDir: o.runDir, base: o.base ?? '', ...o.params }))
  const [cmd, args] = o.params.pin === 'none' ? [cmd0, args0] : pinned(cmd0, args0, String(o.params.pin ?? '2-6'))
  const env = { ...process.env, ...(o.env ?? {}), AWR_PERF_LOCK_HELD: process.env.AWR_PERF_LOCK_HELD ?? 'ex', AWR_PERF_RUN_DIR: o.runDir }
  const r = await runLogged(cmd, args, { env, runDir: o.runDir, name: 'tool', timeoutS: o.timeoutS })
  const st = statusOfExit(r.code, r.timedOut)
  const file = findNewest(o.runDir, 'bench-result.json')
  const bench = file && existsSync(file) ? parseBench(readFileSync(file, 'utf8')) : null
  // bench_cmd keeps its judged medians in bench-ipc.json next to the record (ACC-1: the record has no rtt or gap fields)
  const ipc = join(o.runDir, 'bench-ipc.json')
  if (bench && existsSync(ipc)) bench.ipc = parseBench(readFileSync(ipc, 'utf8'))
  if (!bench && st.execStatus === 'PASS' && (c.metrics ?? []).some((m) => m.source === 'bench')) {
    st.errors.push({ code: 'PERF-E011', message: 'tool wrote no bench-result.json' })
  }
  // tools that print one JSON summary line (rt_client.mjs) feed `script` metrics
  const logFile = join(o.runDir, 'tool.log')
  if (existsSync(logFile) && !existsSync(join(o.runDir, 'metrics.json'))) {
    const last = readFileSync(logFile, 'utf8').trim().split('\n').reverse().find((l) => l.startsWith('{'))
    if (last) {
      try {
        const j = JSON.parse(last)
        const flat = Object.fromEntries(Object.entries(j).filter(([, v]) => typeof v === 'number'))
        if (Object.keys(flat).length) writeFileSync(join(o.runDir, 'metrics.json'), JSON.stringify(flat))
      } catch {
        // not a summary line
      }
    }
  }
  return { ...st, anomaly: false, bench }
}
