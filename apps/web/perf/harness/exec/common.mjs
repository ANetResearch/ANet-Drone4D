// Shared helpers of the four executors (M16-FR-042).
import { spawn } from 'node:child_process'
import { createWriteStream, existsSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { ROOT } from '../registry.mjs'

export const PY = process.env.AWR_PY ?? join(ROOT, '.venv', 'bin', 'python')

/** replace {runDir}, {base}, {n}, {city} ... in command arguments with run values */
export function substitute(args, vars) {
  return args.map((a) => a.replace(/\{(\w+)\}/g, (m, k) => (vars[k] === undefined ? m : String(vars[k]))))
}

/** resolve the interpreter of a command line: python -> .venv python, node stays node */
export function resolveCmd(cmd) {
  const [head, ...rest] = cmd
  if (head === 'python' || head === 'python3') return [PY, rest]
  if (head === 'node') return [process.execPath, rest]
  return [head, rest]
}

/** spawn with a timeout; stdout and stderr go to <runDir>/<name>.log; resolves { code, timedOut, ms } */
export function runLogged(cmd, args, { cwd = ROOT, env = process.env, runDir, name = 'exec', timeoutS = 600 }) {
  return new Promise((ok) => {
    const t0 = Date.now()
    const log = createWriteStream(join(runDir, `${name}.log`))
    const p = spawn(cmd, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
    p.stdout.pipe(log, { end: false })
    p.stderr.pipe(log, { end: false })
    let timedOut = false
    const group = (sig) => {
      if (p.pid === undefined) return
      try {
        process.kill(-p.pid, sig)
      } catch {
        // already gone
      }
    }
    const timer = setTimeout(() => {
      timedOut = true
      group('SIGTERM')
      setTimeout(() => group('SIGKILL'), 5000).unref()
    }, timeoutS * 1000)
    p.on('exit', (code) => {
      clearTimeout(timer)
      log.end()
      ok({ code: code ?? -1, timedOut, ms: Date.now() - t0 })
    })
  })
}

/** newest file with the given name below dir (tools write runs/perf/<tool>-<ts>/bench-result.json below --out) */
export function findNewest(dir, name) {
  let best = null
  const walk = (d) => {
    if (!existsSync(d)) return
    for (const n of readdirSync(d)) {
      const p = join(d, n)
      const st = statSync(p)
      if (st.isDirectory()) walk(p)
      else if (n === name && (!best || st.mtimeMs > best.m)) best = { p, m: st.mtimeMs }
    }
  }
  walk(dir)
  return best?.p ?? null
}

/** exit code of a tool -> executor status (19 §16.2: 6 world invalid, 11 busy, 14 performance environment not ready) */
export function statusOfExit(code, timedOut) {
  if (timedOut) return { execStatus: 'FAIL', errors: [{ code: 'TIMEOUT', message: 'case timed out' }] }
  if (code === 0) return { execStatus: 'PASS', errors: [] }
  if (code === 6) return { execStatus: 'ENV_UNMET', errors: [{ code: 'PERF-E009', message: 'world or data precondition not met (exit 6)' }] }
  if (code === 11) return { execStatus: 'ENV_UNMET', errors: [{ code: 'BUSY', message: 'lock busy or demo runtime active (exit 11)' }] }
  if (code === 14) return { execStatus: 'ENV_UNMET', errors: [{ code: 'PERF-E002', message: 'performance environment not ready (exit 14)' }] }
  return { execStatus: 'FAIL', errors: [{ code: 'EXIT', message: `exit code ${code}` }] }
}
