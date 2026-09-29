// Shared helpers of the M12 browser specs (M12 §10.2; AWR-18 §3, §10): page watchers, the window.__perf.time and
// window.__timeline accessors (test builds only), and a backend with a synthetic recording for the replay cases.
// FakeSource pages reuse the M11 static server (COOP/COEP, /worlds, 404 for /api); replay pages start
// `python -m awr.runtime.supervisor --profile ci --only sim-core,api,replay-worker` with AWR_RUNS_DIR pointing at a temporary runs
// directory that holds one synthetic recording (`python -m awr.recorder.synth`), so `playback{open}` starts the
// on-demand replay-worker through sys/start.
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
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

/** supervisor (ci profile) with a synthetic recording in a temporary runs directory */
export async function startReplayBackend(o: { n?: number; simS?: number } = {}): Promise<ReplayBackend> {
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
  const p: ChildProcess = spawn(py, ['-m', 'awr.runtime.supervisor', '--profile', 'ci', '--only', 'sim-core,api,replay-worker',
    '--set', 'net.port_offset=0', '--set', `net.port=${port}`, '--set', `bus.rendezvous=tcp/127.0.0.1:${bus}`,
    '--set', 'run.keep_run_dir=false'], { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
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
  return {
    url, runs,
    close: async () => {
      if (p.pid) {
        try {
          process.kill(-p.pid, 'SIGTERM')
        } catch {
          // gone
        }
      }
      await new Promise((r) => setTimeout(r, 1500))
      rmSync(runs, { recursive: true, force: true })
    },
  }
}
