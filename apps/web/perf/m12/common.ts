// Shared helpers of the M12 browser specs (M12 §10.2; AWR-18 §3, §10): page watchers, the window.__perf.time and
// window.__timeline accessors (test builds only), and a backend with a synthetic recording for the replay cases.
// FakeSource pages reuse the M11 static server (COOP/COEP, /worlds, 404 for /api); replay pages start
// `python -m awr.runtime.supervisor --profile ci --only sim-core,api,replay-worker` with AWR_RUNS_DIR pointing at a temporary runs
// directory that holds one synthetic recording (`python -m awr.recorder.synth`), so `playback{open}` starts the
// on-demand replay-worker through sys/start. The performance specs (seek-latency, replay20x) pass `profile: 'perf'`: PR-6 CPU
// partitioning (api core0, sim-core core1, replay-worker core7) is on only in the perf profile; with the ci profile the backend
// inherited the Playwright runner's `taskset -c 2-6` and shared cores 2-6 with SwiftShader, which stretched the seek tail to
// 1.4-2.4 s and raised the 20x HOLD ratio (D1 acceptance round 1, FX2-R2-gateway).
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import { cpSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { test, type Page } from '@playwright/test'

export const ROOT = resolve(import.meta.dirname, '../../../..')
export const RUN = 'r20260929-010203-abcd'

export interface PerfTime {
  simNowS: number; tRenderS: number; tFocusS: number; dGlobalMs: number; dFocusMs: number; dWallMs: number; hzEff: number
  rate: number; state4: number; epoch: number; stale: boolean; replay: boolean; frames: number; clockP95Ms: number
  sampleMs: number; sampleP95Ms: number; holdRatio: number; extrapRatio: number; seekMs: number; seekP95Ms: number
}
export interface TimelineHook {
  store: { getState(): Record<string, unknown> }
  actions: Record<string, (...a: unknown[]) => unknown>
  track: { version: number; markers: { n: number }; columns(t0: number, t1: number, w: number): { n: number } }
}

/** utime + stime of a pid in clock ticks (USER_HZ 100; 18 §9.4 item 3), null when unreadable */
export function procTicks(pid: number): number | null {
  try {
    const s = readFileSync(`/proc/${pid}/stat`, 'utf8')
    const f = s.slice(s.lastIndexOf(')') + 2).split(' ')
    return Number(f[11]) + Number(f[12])
  } catch {
    return null
  }
}

/** Authorization header of a viewer token (a viewer never takes the operator seat the page under test needs) */
export async function viewerAuth(url: string): Promise<Record<string, string>> {
  const r = await fetch(`${url}/api/auth/token`, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ role: 'viewer', principal_hint: 'MTWELVEREPLAYVIEWERAAAAA' }) })
  return { authorization: `Bearer ${((await r.json()) as { token: string }).token}` }
}

/** R60 window of perf/server over the last `seconds` (clamped to [5, 600]) */
export async function perfWindow(url: string, auth: Record<string, string>, seconds: number):
  Promise<{ fields?: Record<string, { p50?: number; p99?: number; max?: number }> }> {
  const w = Math.max(5, Math.min(600, Math.round(seconds)))
  return (await (await fetch(`${url}/api/sys/perf?window_s=${w}`, { headers: auth })).json()) as never
}

export function watch(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`${e.name}: ${e.message}`))
  return errors
}

export async function perfTime(page: Page): Promise<PerfTime | null> {
  return page.evaluate(() => ((window as unknown as { __perf?: { time?: unknown } }).__perf?.time ?? null) as never)
}

/** skip when the build has no test switches (window.__timeline) */
export async function needTestBuild(page: Page): Promise<void> {
  const ok = await page.waitForFunction(() => '__timeline' in window, null, { timeout: 30_000 }).then(() => true, () => false)
  test.skip(!ok, 'needs a test build (VITE_AWR_TEST_SWITCHES=1) with window.__timeline')
}

async function freePort(): Promise<number> {
  return new Promise((ok, fail) => {
    const s = createServer()
    s.listen(0, '127.0.0.1', () => {
      const a = s.address()
      const port = typeof a === 'object' && a ? a.port : 0
      s.close(() => ok(port))
    })
    s.on('error', fail)
  })
}

export interface ReplayBackend { url: string; runs: string; close(): Promise<void> }

/** supervisor (ci profile; `perf` for the performance specs) with a synthetic recording in a temporary runs directory */
export async function startReplayBackend(o: { n?: number; simS?: number; profile?: 'ci' | 'perf' } = {}): Promise<ReplayBackend> {
  const py = join(ROOT, '.venv/bin/python')
  if (!existsSync(py)) throw new Error(`${py} not found (make setup)`)
  const runs = mkdtempSync(join(tmpdir(), 'awr-m12-runs-'))
  const syn = spawnSync(py, ['-m', 'awr.recorder.synth', '--out', join(runs, RUN), '--n', String(o.n ?? 60), '--sim-s', String(o.simS ?? 60)],
    { cwd: ROOT, env: { ...process.env, PYTHONPATH: join(ROOT, 'python') }, encoding: 'utf8' })
  if (syn.status !== 0) throw new Error(`synth failed: ${syn.stderr}`)
  const port = await freePort()
  const bus = await freePort()
  const env: Record<string, string | undefined> = { ...process.env, AWR_RUNS_DIR: runs, PYTHONUNBUFFERED: '1' }
  delete env.AWR_SUPERVISOR_PID
  // M12_BACKEND_PROFILE overrides the profile for diagnostics only (e.g. the unpinned ci profile for a before/after comparison)
  const profile = process.env.M12_BACKEND_PROFILE ?? o.profile ?? 'ci'
  const p: ChildProcess = spawn(py, ['-m', 'awr.runtime.supervisor', '--profile', profile, '--only', 'sim-core,api,replay-worker',
    '--set', 'net.port_offset=0', '--set', `net.port=${port}`, '--set', `bus.rendezvous=tcp/127.0.0.1:${bus}`,
    '--set', 'run.keep_run_dir=false'], { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
  // drain the supervisor output: an unread pipe fills after 64 KiB and blocks the supervisor's logging (long replays)
  p.stdout?.resume()
  p.stderr?.resume()
  const url = `http://127.0.0.1:${port}`
  const end = Date.now() + 90_000
  for (;;) {
    try {
      const r = await fetch(`${url}/api/health/ready`)
      if (r.status === 200) break
    } catch {
      // not up yet
    }
    if (Date.now() > end) throw new Error('backend not ready')
    await new Promise((r) => setTimeout(r, 250))
  }
  let exited = p.exitCode !== null
  p.on('exit', () => {
    exited = true
  })
  return {
    url, runs,
    close: async () => {
      // wait for the supervisor to stop its children (they write logs and meta into the runs directory until then), then
      // remove the directory; SIGKILL after 30 s (FX-GW: a fixed 1.5 s wait raced the last writes, ENOTEMPTY)
      const signal = (sig: NodeJS.Signals): void => {
        if (!p.pid) return
        try {
          process.kill(-p.pid, sig)
        } catch {
          // gone
        }
      }
      signal('SIGTERM')
      const end = Date.now() + 30_000
      while (!exited && Date.now() < end) await new Promise((r) => setTimeout(r, 100))
      if (!exited) signal('SIGKILL')
      // diagnostics only: M12_KEEP_LOGS=<dir> keeps the process logs (api.log has the server-side seek timings)
      const keep = process.env.M12_KEEP_LOGS
      if (keep) {
        for (const d of readdirSync(runs)) {
          if (existsSync(join(runs, d, 'logs'))) cpSync(join(runs, d, 'logs'), join(keep, d), { recursive: true })
        }
      }
      rmSync(runs, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 })
    },
  }
}
