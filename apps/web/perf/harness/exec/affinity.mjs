// PR-6 CPU partitioning of the browser under test (AWR-18 §3.1 PR-6; ADR-073 item 6; ACC-4). The Playwright runner and the
// Chromium it starts run under `taskset -c 2-6`, but SwiftShader's marl thread pool sets the affinity of its worker threads
// itself when it creates them (`Thread<00>`..`Thread<07>` in the GPU process; measured 0-4 on this machine, i.e. on the api
// and sim-core cores). While a pw case runs, this guard walks the runner's process tree every 250 ms through
// /proc/<pid>/task/<tid>/children (no full /proc scan) and re-pins every thread whose affinity leaves the browser set, the
// same rule as the Python AffinityGuard of the client-starting tools (tools/bench/ipc/_common.py). Counters go to
// <runDir>/affinity.json. AWR_PERF_AFFINITY_GUARD=0 disables it (diagnostic A/B only; such runs violate PR-6).
import { spawn } from 'node:child_process'
import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

/** parse a cpu list such as '2-6' or '0,2-4' into a Set */
export function parseCpuList(s) {
  const out = new Set()
  for (const part of String(s).trim().split(',')) {
    if (!part) continue
    const [a, b] = part.split('-').map(Number)
    for (let c = a; c <= (b ?? a); c++) out.add(c)
  }
  return out
}

function subset(a, b) {
  for (const x of a) if (!b.has(x)) return false
  return true
}

/** pids of the process tree below root (inclusive), via the per-thread children files */
export function processTree(root) {
  const out = []
  const todo = [root]
  const seen = new Set()
  while (todo.length) {
    const p = todo.pop()
    if (seen.has(p)) continue
    seen.add(p)
    let tids
    try {
      tids = readdirSync(`/proc/${p}/task`)
    } catch {
      continue
    }
    out.push(p)
    for (const t of tids) {
      try {
        const kids = readFileSync(`/proc/${p}/task/${t}/children`, 'utf8').trim()
        if (kids) for (const k of kids.split(/\s+/)) todo.push(Number(k))
      } catch {
        // thread gone
      }
    }
  }
  return out
}

/**
 * Start the guard for the tree below rootPid; returns { stop(): { repinned_threads, scans, errors, cpus, moved_from } }.
 * Inactive (no-op) when disabled or when the cpu list is empty.
 */
export function startAffinityGuard(rootPid, cpus, { periodMs = 250, env = process.env } = {}) {
  const want = parseCpuList(cpus)
  const state = { repinned_threads: 0, scans: 0, errors: 0, cpus, moved_from: {}, active: env.AWR_PERF_AFFINITY_GUARD !== '0' && want.size > 0 }
  if (!state.active || !rootPid) return { stop: () => state }
  const pending = new Set()
  const scan = () => {
    state.scans++
    for (const pid of processTree(rootPid)) {
      let tids
      try {
        tids = readdirSync(`/proc/${pid}/task`)
      } catch {
        continue
      }
      for (const tid of tids) {
        if (pending.has(tid)) continue
        let aff
        try {
          aff = /Cpus_allowed_list:\s*(\S+)/.exec(readFileSync(`/proc/${pid}/task/${tid}/status`, 'utf8'))?.[1]
        } catch {
          continue
        }
        if (!aff || subset(parseCpuList(aff), want)) continue
        pending.add(tid)
        const p = spawn('taskset', ['-p', '-c', cpus, tid], { stdio: 'ignore' })
        p.on('error', () => { state.errors++; pending.delete(tid) })
        p.on('exit', (code) => {
          pending.delete(tid)
          if (code === 0) {
            state.repinned_threads++
            state.moved_from[aff] = (state.moved_from[aff] ?? 0) + 1
          } else state.errors++
        })
      }
    }
  }
  const timer = setInterval(scan, periodMs)
  return {
    stop: () => {
      clearInterval(timer)
      return state
    },
  }
}

/** write the guard counters next to the run artifacts */
export function writeAffinity(runDir, state) {
  try {
    writeFileSync(join(runDir, 'affinity.json'), JSON.stringify(state, null, 1))
  } catch {
    // run dir gone
  }
}
