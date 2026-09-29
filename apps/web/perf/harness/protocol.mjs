// Performance run protocol (AWR-18 §3.1 PR-1 .. PR-12; M16-FR-040; ADR-033).
//   PR-1  exclusive lock $AWR_PERF_LOCK (main repository runs/.perf.lock); make already holds it when
//         AWR_PERF_LOCK_HELD=ex (mk/common.mk with_lock_ex), otherwise run.mjs re-executes itself under `flock -x`.
//   PR-2  before every run: 1 min loadavg <= 4, polled every 10 s, at most 10 min (PERF-E002).
//   PR-4/5 in-run loadavg sampled every 5 s (max for frame pacing, mean for quality and CPU).
//   PR-9  fingerprint (CPU model, cores, Chrome, flags, git) written with every run.
// Time sources are injectable so selftest.mjs can exercise the state machine without waiting.
import { execFileSync, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { cpus, loadavg } from 'node:os'
import { dirname, join } from 'node:path'
import { ROOT } from './registry.mjs'

export const LOAD_START_MAX = 4
export const LOAD_WAIT_S = 600
export const LOAD_POLL_S = 10
export const LOAD_SAMPLE_S = 5
export const LOCK_WAIT_S = 1800

export function lockPath() {
  return process.env.AWR_PERF_LOCK ?? join(ROOT, 'runs', '.perf.lock')
}

/**
 * PR-1: returns null when this process may proceed (lock held by make or by the re-executed child), otherwise the
 * exit code of the re-executed child (flock -x -w 1800 -E 14 <lock> node <argv>).
 */
export function ensureExclusiveLock(argv = process.argv, env = process.env) {
  if (env.AWR_PERF_LOCK_HELD === 'ex' || argv.includes('--locked') || argv.includes('--no-lock')) return null
  const lock = lockPath()
  mkdirSync(dirname(lock), { recursive: true })
  const r = spawnSync('flock', ['-x', '-w', String(LOCK_WAIT_S), '-E', '14', lock, process.execPath, ...argv.slice(1), '--locked'],
    { stdio: 'inherit', env: { ...env, AWR_PERF_LOCK_HELD: 'ex' } })
  if (r.status === 14) console.error(`PERF-E001 LOCK_TIMEOUT: ${lock} not acquired within ${LOCK_WAIT_S} s; 修复：等待构建与测试结束后重试`)
  return r.status ?? 1
}

/** PR-1 probe without waiting (selftest): true when an exclusive lock can be taken right now */
export function lockFree(path = lockPath()) {
  mkdirSync(dirname(path), { recursive: true })
  return spawnSync('flock', ['-x', '-n', '-E', '75', path, 'true']).status === 0
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** PR-2: wait until the 1 min loadavg is <= max; returns { ok, load, waitedS } */
export async function waitLoad({ max = LOAD_START_MAX, timeoutS = LOAD_WAIT_S, pollS = LOAD_POLL_S, read = () => loadavg()[0],
  sleepMs = sleep, now = () => Date.now() } = {}) {
  const t0 = now()
  for (;;) {
    const load = read()
    if (load <= max) return { ok: true, load, waitedS: (now() - t0) / 1000 }
    if ((now() - t0) / 1000 >= timeoutS) return { ok: false, load, waitedS: (now() - t0) / 1000 }
    await sleepMs(pollS * 1000)
  }
}

/** PR-4 / PR-5: loadavg sampled every 5 s while a run is in progress */
export class LoadSampler {
  constructor({ everyS = LOAD_SAMPLE_S, read = () => loadavg()[0] } = {}) {
    this.everyS = everyS
    this.read = read
    this.samples = []
    this.timer = null
  }
  start() {
    this.samples.push(this.read())
    this.timer = setInterval(() => this.samples.push(this.read()), this.everyS * 1000)
    this.timer.unref?.()
    return this
  }
  stop() {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
    this.samples.push(this.read())
    const s = this.samples
    return { max: Math.max(...s), mean: s.reduce((a, b) => a + b, 0) / s.length, series: s }
  }
}

function git(args) {
  try {
    return execFileSync('git', ['-C', ROOT, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
  } catch {
    return null
  }
}

export function gitInfo() {
  const sha = git(['rev-parse', 'HEAD'])
  return { sha: sha ?? 'nogit', branch: git(['rev-parse', '--abbrev-ref', 'HEAD']) ?? 'none', dirty: sha ? git(['status', '--porcelain']) !== '' : false }
}

/** runId = p<YYYYMMDD>-<HHMMSS>-<short sha> (18 §11.1); without git a random 4-hex tag keeps the schema pattern */
export function newRunId(date = new Date(), sha = gitInfo().sha) {
  const p = (n, w = 2) => String(n).padStart(w, '0')
  const d = `${date.getFullYear()}${p(date.getMonth() + 1)}${p(date.getDate())}-${p(date.getHours())}${p(date.getMinutes())}${p(date.getSeconds())}`
  const tag = /^[0-9a-f]{7,}$/.test(sha) ? sha.slice(0, 7) : Math.floor(Date.now() % 0xffff).toString(16).padStart(4, '0')
  return `p${d}-${tag}`
}

export const CHROME = process.env.PW_CHROME ?? `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`

export function chromeVersion() {
  if (!existsSync(CHROME)) return 'missing'
  try {
    return execFileSync(CHROME, ['--version'], { encoding: 'utf8', timeout: 10_000 }).trim()
  } catch {
    return 'unknown'
  }
}

/** PR-9 machine fingerprint (baseline comparison needs cpu_model, cores, chrome and flags to match) */
export function fingerprint(flags = []) {
  const c = cpus()
  let py = 'unknown'
  try {
    py = execFileSync(join(ROOT, '.venv', 'bin', 'python'), ['--version'], { encoding: 'utf8' }).trim()
  } catch {
    // no venv
  }
  return { cpu_model: c[0]?.model ?? 'unknown', cores: c.length, chrome: chromeVersion(), flags, node: process.version, python: py }
}

/** CPU affinity list of a pid (PR-6 check, PERF-E014); e.g. '0' or '2-6' */
export function affinityOf(pid) {
  try {
    const s = readFileSync(`/proc/${pid}/status`, 'utf8')
    return /Cpus_allowed_list:\s*(\S+)/.exec(s)?.[1] ?? null
  } catch {
    return null
  }
}

/** utime + stime of a pid in clock ticks (18 §9.4 item 3) */
export function procTicks(pid) {
  try {
    const s = readFileSync(`/proc/${pid}/stat`, 'utf8')
    const f = s.slice(s.lastIndexOf(')') + 2).split(' ')
    return Number(f[11]) + Number(f[12])
  } catch {
    return null
  }
}

export function procRssMb(pid) {
  try {
    const s = readFileSync(`/proc/${pid}/status`, 'utf8')
    const kb = Number(/VmRSS:\s*(\d+)/.exec(s)?.[1])
    return Number.isFinite(kb) ? kb / 1024 : null
  } catch {
    return null
  }
}
