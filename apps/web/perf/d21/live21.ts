// Live D2.1 backend of the WP-13b Playwright specs (M16; AWR-04 §15.14). The real stack of tests/e2e/sbstack.py (supervisor
// `sandbox` profile: shared show, show-api, sandbox-pool, sandbox-api; the pool spawns a real sim-core per session) started
// with the D2.1 test configuration: power thresholds (AWR_POWER_JSON, read by the pool and published through the public
// capacity endpoint, which the demo build follows), park thresholds (AWR_SANDBOX_JSON), a RELEASE written from the build
// under test (AWR_RELEASE_FILE: the capacity `build` then equals build-info.json field by field) and an internal-identity
// secret file generated at run time (AWR_ADMIN_SECRET_FILE, 0600, random; the deployment pass phrase never appears in the
// repository). R79/R80 are show-api's (WP-03b, delivered in D2-INT); the stand-in of the first round
// (tests/e2e/identity_standin.py, the M17 identity library behind the two routes) is used only with M16_IDENTITY_STANDIN=1
// (to check the specs themselves against an older stack), and the specs record which one answered (annotation
// `identity-backend`) and require show-api otherwise.
// The preview is a private `vite preview` of the build under test routed like the public nginx (live.ts plus the identity
// route). Builds: D21_DIST (test build, VITE_AWR_TEST_SWITCHES=1) and D21_DEMO_DIST (VITE_AWR_DEMO=public); both are built
// into .cache/d21/ when unset (fixed build environment, so the version line is predictable).
import { execFileSync, spawn, type ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { expect, type Browser, type BrowserContext, type Page } from '@playwright/test'
import { preview, type PreviewServer } from 'vite'

export const WEB = resolve(import.meta.dirname, '..', '..')
export const REPO = resolve(WEB, '..', '..')
const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0
const PY = join(REPO, '.venv', 'bin', 'python')
export const PERF = process.env.M16_PERF === '1'

// ---------------------------------------------------------------- builds
export const BUILD_ENV = { AWR_BUILD_SHA: 'abcdef0123', AWR_BUILD_DIRTY: 'false', AWR_BUILD_TIME: '2026-10-06T00:00:00Z', AWR_RELEASE: '20261006-000000-abcdef0123' }
export type BuildKind = 'test' | 'demo'

/** the build under test: D21_DIST / D21_DEMO_DIST, otherwise a private build in .cache/d21/<kind> (never the shared dist/) */
export function ensureBuild(kind: BuildKind): string {
  const given = kind === 'test' ? process.env.D21_DIST ?? process.env.D2_DIST : process.env.D21_DEMO_DIST ?? process.env.DEMO_DIST
  if (given) return resolve(given)
  const out = join(REPO, '.cache', 'd21', kind)
  if (existsSync(join(out, 'index.html')) && existsSync(join(out, 'build-info.json')) && process.env.D21_REBUILD !== '1') return out
  const env: NodeJS.ProcessEnv = { ...process.env, ...BUILD_ENV }
  delete env.VITE_AWR_TEST_SWITCHES
  delete env.VITE_AWR_DEMO
  if (kind === 'test') {
    env.AWR_BUILD_CHANNEL = 'ci'
    env.M15_DIST = out
    execFileSync('node', [join(WEB, 'perf', 'm15', 'helpers', 'build-ui.mjs')], { cwd: WEB, env, stdio: 'pipe' })
  } else {
    env.VITE_AWR_DEMO = 'public'
    execFileSync('npx', ['vite', 'build', '--outDir', out, '--emptyOutDir', '--logLevel', 'warn'], { cwd: WEB, env, stdio: 'pipe' })
  }
  if (!existsSync(join(out, 'index.html'))) throw new Error(`${kind} build missing in ${out}`)
  return out
}

export interface BuildInfo { version: string; channel: string; commit: string; dirty: boolean; built_at: string; release: string }
export const buildInfo = (dist: string): BuildInfo => JSON.parse(readFileSync(join(dist, 'build-info.json'), 'utf8')) as BuildInfo
/** the version line of AWR-04 §15.4.1: `V0.2.0-demo · 3f9a1c2 · 2026-10-12` */
export const display = (b: BuildInfo): string =>
  `V${b.version}-${b.channel} · ${b.commit === 'nogit' ? 'nogit' : b.commit.slice(0, 7)}${b.dirty ? '-dirty' : ''} · ${b.built_at.slice(0, 10)}`

// ---------------------------------------------------------------- the stack
export interface Live21Opts {
  maxSessions?: number
  /** top-level `power:` overrides (seconds), AWR-04 §15.7.2 */
  power?: Record<string, number>
  /** `sandbox.park` overrides */
  park?: Record<string, number>
  /** write RELEASE from this build (build-info.json) */
  releaseFrom?: string
  /** generate an internal-identity secret file (the stand-in only with M16_IDENTITY_STANDIN=1 and a show-api without R79) */
  identity?: boolean
  /** supervisor --set KEY=VALUE */
  set?: string[]
}
export interface Live21 {
  base: string; showBase: string; runId: string; tmp: string; sbxDir: string
  /** R79 endpoint: the stand-in's URL, or null when show-api answers itself */
  identityUrl: string | null
  identityBackend: 'show-api' | 'standin' | 'none'
  /** the run-time secret (identity specs only); never written outside the temporary secret file */
  secret: string | null
  proc: ChildProcess
  stop: () => Promise<void>
}

/** a random secret in a 0600 file under a private temporary directory (AWR-04 §15.9.5 item 2) */
export function makeSecretFile(): { dir: string; path: string; secret: string } {
  const dir = mkdtempSync(join(tmpdir(), 'awr-d21-ident-'))
  const secret = randomBytes(18).toString('base64url')
  const path = join(dir, 'admin.secret')
  writeFileSync(path, `${secret}\n`, { mode: 0o600 })
  chmodSync(path, 0o600)
  return { dir, path, secret }
}

export async function startLive21(o: Live21Opts = {}): Promise<Live21> {
  const args = [join(REPO, 'tests', 'e2e', 'sbstack.py'), '--max-sessions', String(o.maxSessions ?? 1), '--world', 'synthcity']
  for (const kv of o.set ?? []) args.push('--set', kv)
  if (o.power) args.push('--env', `AWR_POWER_JSON=${JSON.stringify(o.power)}`)
  if (o.park) args.push('--env', `AWR_SANDBOX_JSON=${JSON.stringify({ park: o.park })}`)
  if (o.releaseFrom) args.push('--release-from', join(o.releaseFrom, 'build-info.json'))
  let sec: ReturnType<typeof makeSecretFile> | null = null
  if (o.identity) {
    sec = makeSecretFile()
    args.push('--env', `AWR_ADMIN_SECRET_FILE=${sec.path}`, '--env', `AWR_IDENTITY_STATE_DIR=${join(sec.dir, 'state')}`)
    if (process.env.M16_IDENTITY_STANDIN === '1') args.push('--identity-standin')
  }
  const proc = spawn(PY, args, { cwd: REPO, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONUNBUFFERED: '1' } })
  const err: string[] = []
  proc.stderr?.on('data', (d: Buffer) => { err.push(d.toString()); if (err.length > 200) err.shift() })
  const line = await new Promise<string>((ok, fail) => {
    const t = setTimeout(() => fail(new Error(`sandbox stack: no ready line within 300 s\n${err.join('').slice(-3000)}`)), 300_000)
    const rl = createInterface({ input: proc.stdout! })
    rl.on('line', (l) => { if (l.startsWith('{')) { clearTimeout(t); ok(l) } })
    proc.on('exit', (code) => { clearTimeout(t); fail(new Error(`sandbox stack exited ${code}\n${err.join('').slice(-3000)}`)) })
  })
  const j = JSON.parse(line) as { base: string; show_base: string; run_id: string; tmp: string; sbx_dir: string; identity_url: string | null }
  const stop = async (): Promise<void> => {
    if (proc.exitCode === null) {
      proc.stdin?.end()
      await new Promise<void>((ok) => {
        const t = setTimeout(() => { proc.kill('SIGKILL'); ok() }, 60_000)
        proc.on('exit', () => { clearTimeout(t); ok() })
      })
    }
    if (sec) rmSync(sec.dir, { recursive: true, force: true })
  }
  return {
    base: j.base, showBase: j.show_base, runId: j.run_id, tmp: j.tmp, sbxDir: j.sbx_dir, identityUrl: j.identity_url,
    identityBackend: !o.identity ? 'none' : j.identity_url ? 'standin' : 'show-api', secret: sec?.secret ?? null, proc, stop,
  }
}

/** private preview of the build with the nginx-like routing (and the identity stand-in when show-api lacks R79) */
export async function startLive21Preview(port: number, live: Live21, dist: string): Promise<PreviewServer> {
  const proxy: Record<string, { target: string; ws?: boolean }> = {}
  if (live.identityUrl) proxy['/api/auth/identity'] = { target: live.identityUrl }
  proxy['/api/sandbox'] = { target: live.base, ws: true }
  proxy['/api'] = { target: live.showBase, ws: true }
  proxy['^/worlds/.+'] = { target: live.showBase }
  proxy['^/vehicles/.+'] = { target: live.showBase }
  return preview({
    configFile: false, root: WEB, logLevel: 'silent', build: { outDir: dist },
    preview: { host: '127.0.0.1', port: port + 10 * OFFSET, strictPort: true,
      headers: { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp' }, proxy },
  })
}
export const baseUrl = (port: number): string => `http://127.0.0.1:${port + 10 * OFFSET}`

// ---------------------------------------------------------------- sandbox REST (outside the page)
export interface Held { sid: string; token: string; epoch?: number }
export async function createSession(live: Live21, hint: string, template = 't6_manual', identity?: string): Promise<Held> {
  const r = await fetch(`${live.base}/api/sandbox/v1/sessions`, {
    method: 'POST', headers: { 'content-type': 'application/json', ...(identity ? { 'x-awr-identity': identity } : {}) },
    body: JSON.stringify({ world: 'synthcity', template, principal_hint: hint }),
  })
  const j = await r.json() as { session?: { sid: string; epoch?: number }; token?: string }
  if (r.status !== 201 || !j.session || !j.token) throw new Error(`create ${template}: ${r.status} ${JSON.stringify(j).slice(0, 300)}`)
  return { sid: j.session.sid, token: j.token, epoch: j.session.epoch }
}
export async function session(live: Live21, h: Held): Promise<Record<string, unknown>> {
  const r = await fetch(`${live.base}/api/sandbox/v1/sessions/${h.sid}`, { headers: { authorization: `Bearer ${h.token}` } })
  return (await r.json()) as Record<string, unknown>
}
export async function waitState(live: Live21, h: Held, want: string[], timeoutMs = 90_000): Promise<Record<string, unknown>> {
  let last: Record<string, unknown> = {}
  await expect.poll(async () => {
    last = await session(live, h)
    return want.includes(String(last.state))
  }, { timeout: timeoutMs, intervals: [500] }).toBe(true)
  return last
}
export async function endSession(live: Live21, h: Held | null): Promise<void> {
  if (!h) return
  await fetch(`${live.base}/api/sandbox/v1/sessions/${h.sid}`, { method: 'DELETE', headers: { authorization: `Bearer ${h.token}` } }).catch(() => null)
}
export async function capacity(live: Live21): Promise<{ sandbox: Record<string, unknown>; viewers: Record<string, unknown>; build: BuildInfo; hint: string; power: Record<string, number> }> {
  return (await (await fetch(`${live.base}/api/sandbox/v1/capacity`)).json()) as never
}
let showAdmin: { run: string; token: string } | null = null
/** live /api/rt connections of show-api (R58 /api/rt/inspect with the run's admin secret; D2-AC-44 "show-api 连接数") */
export async function showClients(live: Live21): Promise<{ n: number; roles: string[] }> {
  if (!showAdmin || showAdmin.run !== live.runId) {
    const secret = readFileSync(join(live.tmp, 'runs', live.runId, 'admin.token'), 'utf8').trim()
    const r = await fetch(`${live.showBase}/api/auth/token`, { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ role: 'admin', principal_hint: 'M16D21INSPECTHINTAAAAAAA', admin_secret: secret }) })
    const j = await r.json() as { token?: string }
    if (!j.token) throw new Error(`admin token: ${r.status}`)
    showAdmin = { run: live.runId, token: j.token }
  }
  const r = await fetch(`${live.showBase}/api/rt/inspect`, { headers: { authorization: `Bearer ${showAdmin.token}` } })
  const j = await r.json() as { clients?: { role?: string }[] }
  const c = j.clients ?? []
  return { n: c.length, roles: c.map((x) => String(x.role ?? '')) }
}
/** wait until the pool holds no public session (DRAINING still holds a slot) */
export async function waitPoolEmpty(live: Live21, timeoutMs = 60_000): Promise<void> {
  await expect.poll(async () => {
    const c = await capacity(live)
    return Number(c.sandbox.active) === 0 && Number(c.sandbox.queue) === 0
  }, { timeout: timeoutMs, intervals: [500] }).toBe(true)
}
/** the page holds this session (sessionStorage, AWR-14 §20.4.7) and skips the onboarding */
export async function holdSession(page: Page, h: Held): Promise<void> {
  await page.addInitScript(([sid, token, exp]) => {
    if (!sessionStorage.getItem('awr.sandbox.session.v1')) {
      sessionStorage.setItem('awr.sandbox.session.v1', JSON.stringify({ sid, token, token_exp_unix_ms: exp, world: 'synthcity', template: 't1_point_watch' }))
    }
    localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 }))
  }, [h.sid, h.token, Date.now() + 1_800_000] as const)
}
export async function skipOnboarding(page: Page): Promise<void> {
  await page.addInitScript(() => localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 })))
}
/** the session the page holds now (its token changes on resume) */
export async function pageHeld(page: Page): Promise<Held | null> {
  return page.evaluate(() => {
    const v = sessionStorage.getItem('awr.sandbox.session.v1')
    return v ? (JSON.parse(v) as { sid: string; token: string }) : null
  })
}
export function tokenPayload(tok: string): Record<string, unknown> {
  return JSON.parse(Buffer.from(tok.split('.')[1], 'base64url').toString('utf8')) as Record<string, unknown>
}

// ---------------------------------------------------------------- devices (AWR-04 §15.8.1; WP-13b matrix)
export interface Device { name: string; w: number; h: number; dpr: number; ff: 'phone' | 'tablet'; orient: 'portrait' | 'landscape' }
export const DEVICES: readonly Device[] = [
  { name: 'iphone15', w: 393, h: 852, dpr: 3, ff: 'phone', orient: 'portrait' },
  { name: 'iphone15-land', w: 852, h: 393, dpr: 3, ff: 'phone', orient: 'landscape' },
  { name: 'pixel8', w: 412, h: 915, dpr: 2.625, ff: 'phone', orient: 'portrait' },
  { name: 'ipadpro11', w: 834, h: 1194, dpr: 2, ff: 'tablet', orient: 'portrait' },
  { name: 'ipadpro11-land', w: 1194, h: 834, dpr: 2, ff: 'tablet', orient: 'landscape' },
]
const IOS_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
const ANDROID_UA = 'Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36'
const IPAD_UA = 'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
export async function deviceContext(browser: Browser, d: Device, baseURL: string): Promise<BrowserContext> {
  const ua = d.name.startsWith('pixel') ? ANDROID_UA : d.ff === 'tablet' ? IPAD_UA : IOS_UA
  return browser.newContext({ baseURL, viewport: { width: d.w, height: d.h }, deviceScaleFactor: d.dpr, isMobile: true, hasTouch: true, userAgent: ua })
}

// ---------------------------------------------------------------- page checks
export interface Watch { errors: string[]; console: string[]; gl: string[] }
export function watch(page: Page): Watch {
  const w: Watch = { errors: [], console: [], gl: [] }
  page.on('pageerror', (e) => w.errors.push(`${e.name}: ${e.message}`))
  page.on('console', (m) => {
    const t = m.text()
    if (m.type() === 'error' || m.type() === 'warning') {
      if (/gpu-limits|GL_INVALID|WebGL: |THREE\./.test(t)) w.gl.push(t.split('\n')[0])
    }
    if (m.type() !== 'error') return
    w.console.push(t)
    if (/M15-E0\d\d|TypeError|ReferenceError|RangeError/.test(t)) w.errors.push(t.split('\n')[0])
  })
  return w
}
export async function noHorizontalScroll(page: Page): Promise<{ sw: number; cw: number; x: number }> {
  const o = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, x: scrollX }))
  expect(o.sw, 'horizontal overflow').toBeLessThanOrEqual(o.cw)
  expect(o.x).toBe(0)
  return o
}
/** visible controls whose hit area around the centre is under 44 x 44 CSS px (AWR-04 §15.8.5; same audit as perf/m15) */
export async function touchAudit(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const sel = 'button, [role="tab"], [role="switch"], [role="checkbox"], [role="radio"], [role="menuitem"], [role="combobox"], a[href], [data-slot="slider-thumb"], input:not([type="hidden"]):not([aria-hidden="true"])'
    const bad: string[] = []
    for (const el of document.querySelectorAll<HTMLElement>(sel)) {
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) continue
      if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) continue
      if (el.closest('[inert], [aria-hidden="true"], [data-boot], .invisible')) continue
      const cs = getComputedStyle(el)
      if (cs.visibility === 'hidden' || cs.pointerEvents === 'none' || Number(cs.opacity) === 0) continue
      if (r.width >= 43.5 && r.height >= 43.5) continue
      const cx = r.left + r.width / 2
      const cy = r.top + r.height / 2
      const mid = document.elementFromPoint(cx, cy)
      if (!mid || !(mid === el || el.contains(mid) || mid.contains(el))) continue
      const labels = new Set<Element>((el as HTMLElement & { labels?: NodeListOf<HTMLLabelElement> }).labels ?? [])
      if (el.id) for (const l of document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`)) labels.add(l)
      let miss = ''
      for (const [dx, dy] of [[-21, 0], [21, 0], [0, -21], [0, 21]] as const) {
        const h = document.elementFromPoint(Math.min(innerWidth - 1, Math.max(0, cx + dx)), Math.min(innerHeight - 1, Math.max(0, cy + dy)))
        if (!h || !(h === el || el.contains(h) || [...labels].some((l) => l === h || l.contains(h)))) {
          miss = `${dx},${dy} -> ${h ? `${h.tagName.toLowerCase()}.${String(h.className).slice(0, 40)}` : 'none'}`
          break
        }
      }
      if (miss) bad.push(`${el.tagName.toLowerCase()}[${el.getAttribute('data-slot') ?? el.getAttribute('role') ?? ''}] ${(el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 24)} ${Math.round(r.width)}x${Math.round(r.height)} (${miss})`)
    }
    return bad
  })
}
/** D1-AC-20 in the rendered DOM: no emoji or forbidden glyphs */
export async function glyphScan(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const re = /[\p{Extended_Pictographic}\p{Emoji_Presentation}\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]|\u{FE0F}/u
    const bad: string[] = []
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    for (let n = walker.nextNode(); n; n = walker.nextNode()) if (re.test(n.textContent ?? '')) bad.push((n.textContent ?? '').slice(0, 40))
    return bad
  })
}
export const isTestBuild = (page: Page): Promise<boolean> => page.evaluate(() => '__ux' in window && '__uxInject' in window)
export async function revealed(page: Page, timeout = 180_000): Promise<void> {
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout })
}
export async function frames(page: Page, n: number): Promise<void> {
  await page.evaluate((k) => new Promise<void>((ok) => {
    let i = 0
    const step = (): void => { if (++i >= k) ok(); else requestAnimationFrame(step) }
    requestAnimationFrame(step)
  }), n)
}
/** every toast that appears (title and description), recorded from the start of the page (an init script) */
export async function recordToasts(page: Page): Promise<() => Promise<string[]>> {
  await page.addInitScript(() => {
    const log: string[] = []
    ;(window as unknown as { __toastLog: string[] }).__toastLog = log
    const seen = new WeakSet<Element>()
    const scan = (): void => {
      for (const el of document.querySelectorAll('[data-slot="toast"]')) {
        if (seen.has(el)) continue
        const txt = (el.textContent ?? '').trim()
        if (!txt) continue
        seen.add(el)
        log.push(txt)
      }
    }
    new MutationObserver(scan).observe(document, { childList: true, subtree: true, characterData: true })
  })
  return () => page.evaluate(() => (window as unknown as { __toastLog?: string[] }).__toastLog ?? [])
}
export const SHOTS = process.env.AWR_SHOTS_DIR ?? join(REPO, '.cache', 'impl', 'shots')
export async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: join(SHOTS, `${name}.png`), animations: 'disabled' }).catch(() => null)
}
