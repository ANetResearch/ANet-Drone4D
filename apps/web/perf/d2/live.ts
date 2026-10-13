// Live sandbox backend of the WP-13 D2 Playwright specs (M16): the real stack of tests/e2e/sbstack.py (supervisor `sandbox`
// profile: shared-show sim-core, show-api, sandbox-pool, sandbox-api; the pool spawns a real sim-core per session from
// scenarios/sandbox/<world>/<template>.json) behind a private `vite preview` of a test build that routes like the public
// site's nginx (AWR-19 D2): /api/sandbox/ (REST and the per-session WebSocket) to sandbox-api, the rest of /api, /worlds and
// /vehicles to show-api. Builds: D2_DIST (or M15_DIST) selects a private test build; parallel agents never share dist/.
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { preview, type PreviewServer } from 'vite'

export const WEB = resolve(import.meta.dirname, '..', '..')
export const REPO = resolve(WEB, '..', '..')
export const DIST = process.env.D2_DIST ?? process.env.M15_DIST ?? 'dist'
const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0
const PY = join(REPO, '.venv', 'bin', 'python')

export interface LiveStack { base: string; showBase: string; runId: string; proc: ChildProcess; stop: () => Promise<void> }

/** starts tests/e2e/sbstack.py and resolves once it printed its JSON line (sandbox-api healthy) */
export async function startLiveStack(maxSessions = 2, world = 'synthcity', set: string[] = []): Promise<LiveStack> {
  const proc = spawn(PY, [join(REPO, 'tests', 'e2e', 'sbstack.py'), '--max-sessions', String(maxSessions), '--world', world,
    ...set.flatMap((kv) => ['--set', kv])],
    { cwd: REPO, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONUNBUFFERED: '1' } })
  const err: string[] = []
  proc.stderr?.on('data', (d: Buffer) => { err.push(d.toString()); if (err.length > 200) err.shift() })
  const line = await new Promise<string>((ok, fail) => {
    const t = setTimeout(() => fail(new Error(`sandbox stack: no ready line within 240 s\n${err.join('').slice(-3000)}`)), 240_000)
    const rl = createInterface({ input: proc.stdout! })
    rl.on('line', (l) => { if (l.startsWith('{')) { clearTimeout(t); ok(l) } })
    proc.on('exit', (code) => { clearTimeout(t); fail(new Error(`sandbox stack exited ${code}\n${err.join('').slice(-3000)}`)) })
  })
  const j = JSON.parse(line) as { base: string; show_base: string; run_id: string }
  const stop = async () => {
    if (proc.exitCode !== null) return
    proc.stdin?.end()
    await new Promise<void>((ok) => {
      const t = setTimeout(() => { proc.kill('SIGKILL'); ok() }, 60_000)
      proc.on('exit', () => { clearTimeout(t); ok() })
    })
  }
  return { base: j.base, showBase: j.show_base, runId: j.run_id, proc, stop }
}

/** private preview of the test build with the nginx-like routing to the live stack */
export async function startLivePreview(port: number, live: LiveStack, dist: string = DIST): Promise<PreviewServer> {
  return preview({
    configFile: false,
    root: WEB,
    logLevel: 'silent',
    build: { outDir: dist },
    preview: {
      host: '127.0.0.1',
      port: port + 10 * OFFSET,
      strictPort: true,
      headers: { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp' },
      proxy: {
        '/api/sandbox': { target: live.base, ws: true },
        '/api': { target: live.showBase, ws: true },
        '^/worlds/.+': { target: live.showBase },
        '^/vehicles/.+': { target: live.showBase },
      },
    },
  })
}

export const baseUrl = (port: number): string => `http://127.0.0.1:${port + 10 * OFFSET}`

/** all operations of an OpenAPI document (methods in lower case) */
export function countOps(doc: { paths: Record<string, Record<string, unknown>> }): number {
  let n = 0
  for (const item of Object.values(doc.paths)) for (const m of ['get', 'post', 'put', 'patch', 'delete']) if (item[m]) n++
  return n
}

/** merge script metrics into <AWR_PERF_RUN_DIR>/metrics.json (the harness reads it; runs/playwright/perf-debug otherwise) */
export function mergeMetrics(values: Record<string, number | null>): void {
  const dir = process.env.AWR_PERF_RUN_DIR ?? join(REPO, 'runs', 'playwright', 'perf-debug')
  mkdirSync(dir, { recursive: true })
  const f = join(dir, 'metrics.json')
  const cur = existsSync(f) ? (JSON.parse(readFileSync(f, 'utf8')) as Record<string, unknown>) : {}
  writeFileSync(f, JSON.stringify({ ...cur, ...values }))
}
