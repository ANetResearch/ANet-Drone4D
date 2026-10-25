// D3 regression gate, item 6 (AWR-05 §4.8 rule 6; D3-AC-01; AWR-18 §21.2; ADR-194): the D2 public site e2e and the Tier S
// first screen on the public demo build, run by `tools/bench/d3/regress_gate.py --steps public-e2e` (step `public-e2e`).
// The gate starts the backend under the emax cgroup emulation (AWR-04 §13: unit CPUQuota 250 %, MemoryHigh 1.8 GB, MemoryMax
// 2 GB, 4-core cpuset, the §4.4.4 subgroups of tools/deploy/emax/cgroup-exec.sh; supervisor with the shared show S7 and the
// sandbox control plane) and runs this spec on the other cores as the visitor's machine. The site is served like the emax
// nginx: the demo build at /, /api/sandbox/ (REST and the per-session WebSocket) to sandbox-api, the rest of /api, /worlds and
// /vehicles to show-api; with D2PG_LAB_DIST the /lab/ static build is mounted at /lab/ (SPA fallback to /lab/index.html,
// /lab-static/ answers 404), the V0.3 layout of the site.
//   1. landing: the page, both main actions, the capacity line from sandbox-api, no horizontal overflow, no application chunk
//      on the first screen, no console error;
//   2. World Hub: seven world cards (ADR-109), each with its open action;
//   3. shared show as a viewer (/world/synthcity): role badge viewer on the shared surface, a live vehicle in the rail and its
//      detail, no write request (the viewer token of /api/auth/token aside), no manual takeover control;
//   4. visitor path: landing -> "开启我的沙盒" -> start -> /sandbox/<sid> with the session bar and the operator badge; the
//      session is ended through the API afterwards;
//   5. D3 entries: no page of the D2 site requests /lab/ or /api/bench (the run-time half of the K9 D3 extension); with the
//      /lab/ build mounted, /lab/ loads and issues only static GETs below /lab/ and /lab-static/;
//   6. Tier S first screen (AWR-16 §4.11; D1-AC-02): D2PG_REPEATS fresh contexts per world of D2PG_WORLDS, each sample the
//      __perf probe after the reveal (tier, device class, TTFP without the shader warm-up wait as D1-AC-02 defines it, the
//      first pixel, the warm-up wait, the reveal time, first screen bytes, programs at the reveal, programs compiled after it,
//      GL errors, points drawn, failed nodes, budget violations). The gate judges the samples against the D2 baseline
//      (tests/d3/regress_gate/baseline.json, section public_e2e); this spec asserts only what holds on any host: Tier S on
//      SwiftShader, first screen bytes > 0 and equal across repeats, no GL error and no failed node.
// Every test also records its checks into D2PG_OUT (metrics.json, awr.d3.public_e2e.metrics.v1) for the gate report.
// Environment: D2PG_DIST (a VITE_AWR_DEMO=public build, required: the spec skips without it), D2PG_LAB_DIST, D2PG_SANDBOX and
// D2PG_SHOW (sandbox-api and show-api bases; without them the spec starts tests/e2e/sbstack.py itself, no cgroup: a developer
// run, not a gate result), D2PG_OUT, D2PG_WORLDS (default synthcity,shenzhen), D2PG_REPEATS (default 3).
// Production retest after the seal deployment (ADR-194 step 6): D2PG_SITE=<site origin> runs the same cases against a deployed
// site instead of the local preview and stack (D2PG_DIST is then not needed; D2PG_LAB=1 when /lab/ is published there). The
// visitor case then opens and ends one real sandbox session on that site.
// Run directly: D2PG_DIST=.cache/deploy/emax/web-dist AWR_PERF_BASE=http://127.0.0.1:1 npx playwright test d2-public-gate --project e2e
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { createServer } from 'node:net'
import { dirname, extname, join, normalize, resolve, sep } from 'node:path'
import { expect, test, type Browser, type BrowserContext, type Page } from '@playwright/test'
import { preview, type PreviewServer } from 'vite'
import { startLiveStack, type LiveStack } from '../../apps/web/perf/d2/live'

const REPO = resolve(import.meta.dirname, '..', '..')
const WEB = join(REPO, 'apps', 'web')
const abs = (p: string | undefined): string => (p ? resolve(REPO, p) : '')
const DIST = abs(process.env.D2PG_DIST)
const LAB_DIST = abs(process.env.D2PG_LAB_DIST)
const OUT = abs(process.env.D2PG_OUT) || join(REPO, 'runs', 'playwright', 'd2-public-gate', 'metrics.json')
const WORLDS = (process.env.D2PG_WORLDS ?? 'synthcity,shenzhen').split(',').map((s) => s.trim()).filter(Boolean)
const REPEATS = Math.max(1, Number.parseInt(process.env.D2PG_REPEATS ?? '3', 10) || 3)
const SITE = (process.env.D2PG_SITE ?? '').replace(/\/$/, '')
const LAB = SITE ? process.env.D2PG_LAB === '1' : !!LAB_DIST && existsSync(join(LAB_DIST, 'index.html'))
/** the seven worlds of the public site (ADR-109; apps/web/src/lib/demo.ts DEMO_WORLDS) */
const SEVEN = ['synthcity', 'shenzhen', 'suzhou', 'newyork', 'chicago', 'shanghai', 'sanfrancisco']
const ONBOARDED = JSON.stringify({ done: true, step: 3, v: 1 })

// ------------------------------------------------------------ metrics file (read by the gate)
type Check = { test: string; ok: boolean; detail?: unknown }
interface TierSample {
  world: string; repeat: number; tier: string; deviceClass: string; ttfp: number | null; ttfpFirstPixel: number | null; warmupWaitMs: number
  tti: number | null; firstScreenBytes: number
  programsAtReveal: number; compiledAfterReveal: number; glErrors: number; drawn: number; failed: number; budgetViolations: number
}
const metrics = {
  schema: 'awr.d3.public_e2e.metrics.v1', started_utc: new Date().toISOString(), dist: SITE || DIST, lab_dist: LAB ? (SITE ? `${SITE}/lab/` : LAB_DIST) : null,
  backend: SITE ? 'site' : process.env.D2PG_SHOW ? 'gate' : 'self', worlds: WORLDS, repeats: REPEATS, viewport: [1280, 720],
  checks: [] as Check[], tier_s: [] as TierSample[], d3_requests: [] as string[], build_info: null as unknown,
}
function flush(): void {
  mkdirSync(dirname(OUT), { recursive: true })
  writeFileSync(OUT, JSON.stringify(metrics, null, 1) + '\n')
}
const record = (name: string, ok: boolean, detail?: unknown): void => {
  metrics.checks.push({ test: name, ok, ...(detail === undefined ? {} : { detail }) })
  flush()
}

// ------------------------------------------------------------ site (emax nginx routing in a private vite preview)
async function freePort(): Promise<number> {
  return new Promise((ok, fail) => {
    const s = createServer()
    s.once('error', fail)
    s.listen(0, '127.0.0.1', () => {
      const a = s.address()
      const p = typeof a === 'object' && a ? a.port : 0
      s.close(() => ok(p))
    })
  })
}

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp',
  '.woff2': 'font/woff2', '.ico': 'image/x-icon', '.txt': 'text/plain; charset=utf-8', '.wasm': 'application/wasm',
}

/** /lab/ like the emax site file (try_files $uri /lab/index.html) and /lab-static/ without published artifacts (404) */
function labMount(dir: string) {
  return (req: IncomingMessage, res: ServerResponse, next: () => void): void => {
    const path = decodeURIComponent((req.url ?? '/').split('?')[0])
    if (path.startsWith('/lab-static/')) {
      res.statusCode = 404
      res.end()
      return
    }
    if (path !== '/lab' && !path.startsWith('/lab/')) {
      next()
      return
    }
    const rel = normalize(path.slice('/lab'.length).replace(/^\/+/, ''))
    let file = join(dir, rel)
    if (!file.startsWith(dir + sep) || !existsSync(file) || statSync(file).isDirectory()) file = join(dir, 'index.html')
    res.setHeader('content-type', MIME[extname(file)] ?? 'application/octet-stream')
    res.setHeader('cache-control', 'no-cache')
    res.end(readFileSync(file))
  }
}

async function startSite(port: number, sandbox: string, show: string): Promise<PreviewServer> {
  return preview({
    configFile: false,
    root: WEB,
    logLevel: 'silent',
    build: { outDir: DIST },
    plugins: LAB ? [{ name: 'd2pg-lab-mount', configurePreviewServer: (s) => { s.middlewares.use(labMount(LAB_DIST)) } }] : [],
    preview: {
      host: '127.0.0.1',
      port,
      strictPort: true,
      headers: { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp' },
      proxy: {
        '/api/sandbox': { target: sandbox, ws: true },
        '/api': { target: show, ws: true },
        '^/worlds/.+': { target: show },
        '^/vehicles/.+': { target: show },
      },
    },
  })
}

// ------------------------------------------------------------ page helpers
/** D3 entry paths no page of the D2 site may request (the run-time half of the K9 D3 extension, AWR-05 §4.8 rule 6) */
const D3_PATH = /^\/(lab|lab-static)(\/|$)|^\/api\/bench(\/|$)/
interface Watch { errors: string[]; gl: string[]; requests: string[]; writes: string[] }

function watchPage(page: Page): Watch {
  const w: Watch = { errors: [], gl: [], requests: [], writes: [] }
  page.on('pageerror', (e) => {
    const m = `pageerror ${e.name}: ${e.message}`
    if (!/WebGPU is not available/i.test(m)) w.errors.push(m)
  })
  page.on('console', (m) => {
    const t = m.text().split('\n')[0]
    if (/\[gpu-limits\]|GL_INVALID|GL ERROR|WebGL: /.test(t)) w.gl.push(t)
    else if (m.type() === 'error' && !/WebGPU/i.test(t)) w.errors.push(t)
  })
  page.on('request', (r) => {
    const u = new URL(r.url())
    w.requests.push(`${r.method()} ${u.pathname}`)
    // the page the request comes from: a navigation counts as the page it opens (page.url() is still the previous one)
    const from = r.isNavigationRequest() && r.frame() === page.mainFrame() ? u.pathname : new URL(page.url() || 'about:blank', 'http://x').pathname
    if (D3_PATH.test(u.pathname) && !/^\/lab(\/|$)/.test(from)) metrics.d3_requests.push(`${from} -> ${r.method()} ${u.pathname}`)
    if (!['GET', 'HEAD', 'OPTIONS'].includes(r.method()) && u.pathname !== '/api/auth/token') w.writes.push(`${r.method()} ${u.pathname}`)
  })
  return w
}

async function newPage(browser: Browser, onboarded = true): Promise<{ ctx: BrowserContext; page: Page; w: Watch }> {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1, locale: 'zh-CN' })
  if (onboarded) await ctx.addInitScript((v) => localStorage.setItem('awr.ui.onboarding.v1', v), ONBOARDED)
  const page = await ctx.newPage()
  return { ctx, page, w: watchPage(page) }
}

async function revealed(page: Page, timeout = 180_000): Promise<void> {
  await page.waitForFunction(() => {
    const p = (window as unknown as { __perf?: { load?: { revealAt: number; ttfp: number } } }).__perf
    return !!p?.load && p.load.revealAt > 0 && Number.isFinite(p.load.ttfp) && p.load.ttfp > 0
  }, null, { polling: 250, timeout })
}

/** ends a sandbox session the page holds (sessionStorage awr.sandbox.session.v1) through the API */
async function endSession(page: Page): Promise<boolean> {
  const h = await page.evaluate(() => sessionStorage.getItem('awr.sandbox.session.v1')).catch(() => null)
  if (!h) return false
  const { sid, token } = JSON.parse(h) as { sid: string; token: string }
  const st = await page.evaluate(async ([s, t]) => (await fetch(`/api/sandbox/v1/sessions/${s}`, { method: 'DELETE', headers: { authorization: `Bearer ${t}` } })).status, [sid, token])
  return st >= 200 && st < 300
}

// ------------------------------------------------------------ lifecycle
let live: LiveStack | null = null
let site: PreviewServer | null = null
let base = ''

test.describe.configure({ mode: 'serial' })
test.skip(!SITE && (!DIST || !existsSync(join(DIST, 'index.html'))), 'needs D2PG_DIST (a VITE_AWR_DEMO=public build, e.g. make demo-build) or D2PG_SITE')

test.beforeAll(async () => {
  test.setTimeout(420_000)
  if (SITE) {
    base = SITE
    const r = await fetch(`${SITE}/build-info.json`).catch(() => null)
    metrics.build_info = r?.ok ? await r.json() : null
    flush()
    return
  }
  const bi = join(DIST, 'build-info.json')
  metrics.build_info = existsSync(bi) ? JSON.parse(readFileSync(bi, 'utf8')) : null
  let sandbox = process.env.D2PG_SANDBOX ?? ''
  let show = process.env.D2PG_SHOW ?? ''
  if (!sandbox || !show) {
    live = await startLiveStack(2, 'synthcity', ['run.scenario=s7-synthcity-relay', 'run.scenario_profile=public'])
    sandbox = live.base
    show = live.showBase
  }
  const port = await freePort()
  site = await startSite(port, sandbox, show)
  base = `http://127.0.0.1:${port}`
  flush()
})

test.afterAll(async () => {
  flush()
  await site?.close()
  await live?.stop()
})

// ------------------------------------------------------------ 1. landing
test('landing: page, main actions, capacity, first screen without application chunks', async ({ browser }) => {
  test.setTimeout(180_000)
  const { ctx, page, w } = await newPage(browser)
  try {
    await page.goto(`${base}/`)
    await expect(page.locator('[data-view="landing"]')).toBeVisible({ timeout: 60_000 })
    await expect(page.locator('h1')).toBeVisible()
    await expect(page.locator('[data-demo-launch]')).toHaveAttribute('href', '/world/synthcity')
    await expect(page.locator('[data-sandbox-open]')).toBeEnabled({ timeout: 60_000 })
    await expect(page.locator('[data-landing-capacity]')).toHaveAttribute('data-landing-capacity', /^(open|busy|full|queue)/, { timeout: 30_000 })
    for (const id of ['story', 'showcase', 'closing', 'open-source']) await expect(page.locator(`#${id}`)).toHaveCount(1)
    await page.waitForTimeout(1000)
    const sw = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
    expect(sw, 'horizontal overflow').toBeLessThanOrEqual(0)
    const js = w.requests.filter((r) => r.endsWith('.js')).map((r) => r.split('/').pop()!.replace(/-[A-Za-z0-9_-]{8}\.js$/, ''))
    const app = js.filter((n) => ['three', 'ui', 'engine', 'main', 'r3f', 'state', 'net'].includes(n))
    expect(app, 'application chunks on the landing first screen').toEqual([])
    expect(w.errors).toEqual([])
    expect(w.writes).toEqual([])
    record('landing', true, { chunks: js.length })
  } catch (e) {
    record('landing', false, String(e).slice(0, 400))
    throw e
  } finally {
    await ctx.close()
  }
})

// ------------------------------------------------------------ 2. World Hub
test('World Hub: the seven public worlds', async ({ browser }) => {
  test.setTimeout(180_000)
  const { ctx, page, w } = await newPage(browser)
  try {
    await page.goto(`${base}/worlds`)
    await expect(page.locator('[data-view="world-hub"]')).toBeVisible({ timeout: 90_000 })
    await expect(page.locator('[data-world-cards]')).toHaveAttribute('data-world-cards', String(SEVEN.length), { timeout: 60_000 })
    for (const id of SEVEN) await expect(page.locator(`[data-world-open="${id}"]`)).toBeEnabled()
    expect(w.errors).toEqual([])
    expect(w.writes).toEqual([])
    record('world-hub', true, { worlds: SEVEN.length })
  } catch (e) {
    record('world-hub', false, String(e).slice(0, 400))
    throw e
  } finally {
    await ctx.close()
  }
})

// ------------------------------------------------------------ 3. shared show viewer
test('shared show viewer: role badge, live vehicle and detail, no write request', async ({ browser }) => {
  test.setTimeout(300_000)
  const { ctx, page, w } = await newPage(browser)
  try {
    await page.goto(`${base}/world/synthcity`)
    await revealed(page)
    await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 120_000 })
    const badge = page.locator('[data-demo-role]').first()
    await expect(badge).toHaveAttribute('data-surface', 'shared', { timeout: 30_000 })
    await expect(badge).toHaveAttribute('data-role', /viewer/)
    await page.locator('[data-entity="drones"]').click()
    const row = page.locator('[data-drone-id]').first()
    await expect(row).toBeVisible({ timeout: 90_000 })
    await row.click()
    await expect(page.locator('[data-drone-detail]').first()).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('[data-manual-takeover]')).toHaveCount(0)
    await page.keyboard.press('Escape')
    await page.waitForTimeout(3000)
    expect(w.writes, w.writes.join(', ')).toEqual([])
    expect(w.errors).toEqual([])
    expect(w.gl).toEqual([])
    record('shared-show-viewer', true, { drones: await page.locator('[data-drone-id]').count() })
  } catch (e) {
    record('shared-show-viewer', false, String(e).slice(0, 400))
    throw e
  } finally {
    await ctx.close()
  }
})

// ------------------------------------------------------------ 4. visitor path into a sandbox
test('visitor path: landing to an own sandbox in two clicks', async ({ browser }) => {
  test.setTimeout(360_000)
  const { ctx, page, w } = await newPage(browser)
  let ended = false
  try {
    await page.goto(`${base}/`)
    const open = page.locator('[data-sandbox-open]')
    await expect(open).toBeEnabled({ timeout: 60_000 })
    await open.click()
    await expect(page.locator('[data-start-sheet]')).toBeVisible()
    await expect(page.locator('[data-template="t5_area_guard"]')).toHaveAttribute('data-checked', '', { timeout: 30_000 })
    await page.locator('[data-start-go]').click()
    await expect(page).toHaveURL(/\/sandbox\/sb-[a-z2-7]{10}/, { timeout: 120_000 })
    await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 150_000 })
    await expect(page.locator('[data-demo-role][data-surface="sandbox"]')).toBeVisible()
    await revealed(page)
    ended = await endSession(page)
    expect(ended, 'session ended through the API').toBe(true)
    expect(w.errors).toEqual([])
    record('visitor-sandbox', true, { url: new URL(page.url()).pathname.replace(/sb-[a-z2-7]{10}/, 'sb-*') })
  } catch (e) {
    if (!ended) await endSession(page).catch(() => false)
    record('visitor-sandbox', false, String(e).slice(0, 400))
    throw e
  } finally {
    await ctx.close()
  }
})

// ------------------------------------------------------------ 5. the /lab/ static build next to the D2 site
test('/lab/ static build: loads with static GETs only (V0.3 layout)', async ({ browser }) => {
  test.skip(!LAB, 'no /lab/ build mounted (D2PG_LAB_DIST, or D2PG_LAB=1 with D2PG_SITE)')
  test.setTimeout(120_000)
  const { ctx, page, w } = await newPage(browser)
  try {
    await page.goto(`${base}/lab/`)
    await page.waitForLoadState('networkidle')
    await expect(page.locator('body')).not.toBeEmpty()
    const bad = w.requests.filter((r) => !/^GET \/(lab|lab-static)(\/|$)/.test(r))
    expect(bad, 'requests outside GET /lab/** and /lab-static/**').toEqual([])
    expect(w.errors.filter((e) => !/404|Failed to load resource/.test(e))).toEqual([])
    record('lab-static', true, { requests: w.requests.length })
  } catch (e) {
    record('lab-static', false, String(e).slice(0, 400))
    throw e
  } finally {
    await ctx.close()
  }
})

// ------------------------------------------------------------ 6. Tier S first screen
test('Tier S first screen: TTFP, first screen bytes and programs per world', async ({ browser }) => {
  test.setTimeout(240_000 * WORLDS.length * REPEATS)
  const problems: string[] = []
  for (const world of WORLDS) {
    for (let k = 0; k < REPEATS; k++) {
      const { ctx, page, w } = await newPage(browser)
      try {
        await page.goto(`${base}/world/${world}`)
        await revealed(page)
        await page.waitForTimeout(1500) // drawn points and programs settle one governor phase after the reveal
        const s = await page.evaluate(() => {
          type L = { ttfp: number; ttfpFirstPixel: number; warmupWaitMs: number; tti: number; firstScreenBytes: number }
          const p = (window as unknown as { __perf: { meta: { tier: string; deviceClass: string }; load: L; gpu: { programsAtReveal: number; compiledAfterReveal: number; glErrors: number }; pc: { drawn: number; failed: number; budgetViolations: number } } }).__perf
          const num = (v: number): number | null => (Number.isFinite(v) ? v : null)
          return { tier: p.meta.tier, deviceClass: p.meta.deviceClass, ttfp: num(p.load.ttfp), ttfpFirstPixel: num(p.load.ttfpFirstPixel),
            warmupWaitMs: p.load.warmupWaitMs, tti: num(p.load.tti), firstScreenBytes: p.load.firstScreenBytes,
            programsAtReveal: p.gpu.programsAtReveal, compiledAfterReveal: p.gpu.compiledAfterReveal, glErrors: p.gpu.glErrors, drawn: p.pc.drawn,
            failed: p.pc.failed, budgetViolations: p.pc.budgetViolations }
        })
        metrics.tier_s.push({ world, repeat: k, ...s })
        flush()
        if (s.tier !== 'S' || s.deviceClass !== 'software') problems.push(`${world}#${k}: tier ${s.tier}/${s.deviceClass}`)
        if (!(s.firstScreenBytes > 0)) problems.push(`${world}#${k}: first screen bytes ${s.firstScreenBytes}`)
        if (s.glErrors || w.gl.length) problems.push(`${world}#${k}: GL errors ${s.glErrors} ${w.gl.slice(0, 2).join(' | ')}`)
        if (s.failed) problems.push(`${world}#${k}: failed nodes ${s.failed}`)
        if (w.errors.length) problems.push(`${world}#${k}: ${w.errors.slice(0, 2).join(' | ')}`)
      } finally {
        await ctx.close()
      }
    }
    const bytes = new Set(metrics.tier_s.filter((s) => s.world === world).map((s) => s.firstScreenBytes))
    if (bytes.size !== 1) problems.push(`${world}: first screen bytes differ between repeats ${[...bytes].join(', ')}`)
  }
  record('tier-s', problems.length === 0, problems.length ? problems : undefined)
  expect(problems).toEqual([])
})

// ------------------------------------------------------------ 7. D3 entries over the whole run
test('D3 entries: no page of the D2 site requested /lab/, /lab-static/ or /api/bench', async () => {
  const ok = metrics.d3_requests.length === 0
  record('d3-requests', ok, ok ? undefined : metrics.d3_requests.slice(0, 20))
  expect(metrics.d3_requests).toEqual([])
})
