// D2.2-L1: the public demo landing page on the devices it is designed for (desktop 1920 x 1080, iPhone 15, Pixel 8, iPad Pro
// 11 portrait and landscape; Chromium device emulation, flag set C1 so WebGL2 runs on SwiftShader).
// Asserts per device: no horizontal overflow (top and bottom of the page), no console error or page error, both main actions
// actionable (trial click: visible, enabled, receiving events), touch targets >= 44 CSS px below 1280 px, the voxel backdrop
// on WebGL2. Further cases: the mobile Sheet menu scrolls to a section; the first screen loads no application chunk (three,
// ui, engine, main) and the backdrop renderer only as its lazy chunk; the WebGL2 specification minimum limits (desktop
// `min`, phone `mobile` with an Adreno renderer string) give no WebGL error or [gpu-limits] refusal; prefers-reduced-motion
// renders one still frame and shows every section; without WebGL2 the Canvas2D renderer runs; the loop pauses on a hidden
// page and after 9 s without input, and resumes on scroll.
// Build: D22_DEMO_DIST, otherwise a private VITE_AWR_DEMO=public build in .cache/d22/demo (rebuilt when a landing source is
// newer). Server: the M06 test server (static build, /api answers 404; the capacity request is routed per context).
// Run: AWR_PORT_OFFSET=<n> AWR_PERF_BASE=http://127.0.0.1:1 npx playwright test perf/d22/landing.spec.ts --project perf
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { devices, expect, test, type Browser, type BrowserContextOptions, type Page } from '@playwright/test'
import { startM06Server, type M06Server } from '../m06/server'
import { ADRENO, emulate, MIN, MOBILE } from '../d21/limits'

const WEB = resolve(import.meta.dirname, '..', '..')
const REPO = resolve(WEB, '..', '..')

function newest(paths: string[]): number {
  let t = 0
  for (const p of paths) {
    if (!existsSync(p)) continue
    const st = statSync(p)
    if (st.isDirectory()) t = Math.max(t, newest(readdirSync(p).map((n) => join(p, n))))
    else t = Math.max(t, st.mtimeMs)
  }
  return t
}

function ensureDemo(): string {
  if (process.env.D22_DEMO_DIST) return resolve(process.env.D22_DEMO_DIST)
  const out = join(REPO, '.cache', 'd22', 'demo')
  const html = join(out, 'index.html')
  const src = newest([join(WEB, 'src', 'app', 'demo'), join(WEB, 'src', 'app', 'i18n'), join(WEB, 'src', 'styles'), join(WEB, 'vite.config.ts')])
  if (existsSync(html) && statSync(html).mtimeMs > src) return out
  const env: NodeJS.ProcessEnv = { ...process.env, VITE_AWR_DEMO: 'public', AWR_BUILD_SHA: 'abcdef0123', AWR_BUILD_DIRTY: 'false',
    AWR_BUILD_TIME: '2026-10-13T00:00:00Z', AWR_RELEASE: '20261013-000000-abcdef0123' }
  delete env.VITE_AWR_TEST_SWITCHES
  execFileSync('npx', ['vite', 'build', '--outDir', out, '--emptyOutDir', '--logLevel', 'warn'], { cwd: WEB, env, stdio: 'pipe' })
  return out
}

const CAPACITY = (): Record<string, unknown> => ({
  schema: 'awr.capacity.v1', t_unix_ns: String(BigInt(Date.now()) * 1_000_000n),
  build: { version: '0.2.0', channel: 'demo', commit: 'abcdef0123', dirty: false, built_at: '2026-10-13T00:00:00Z', release: '20261013-000000-abcdef0123' },
  accepting: true, maintenance: false, degrade_level: 'L0',
  viewers: { live: 3, max: 24, queue: 0, eta_s: null, state: 'open' },
  sandbox: { active: 1, max: 2, reserved_internal: 1, internal_active: 0, parked: 0, queue: 0, eta_s: 0, admit_now: true, worlds_admit_now: ['synthcity'], state: 'open' },
  power: { viewer_idle_s: 300, sandbox_idle_s: 600, hidden_s: 60, warn_s: 30 }, hint: 'open',
})

type Device = 'desktop' | 'iphone15' | 'pixel8' | 'ipad11' | 'ipad11l'
const strip = (d: (typeof devices)[string]): BrowserContextOptions => {
  const { defaultBrowserType: _b, ...rest } = d
  return rest
}
const DEVICES: Record<Device, BrowserContextOptions> = {
  desktop: { viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
  iphone15: strip(devices['iPhone 15']),
  pixel8: strip(devices['Pixel 8']),
  ipad11: strip(devices['iPad Pro 11']),
  ipad11l: strip(devices['iPad Pro 11 landscape']),
}

let server: M06Server | null = null
test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => {
  test.setTimeout(600_000)
  server = await startM06Server(undefined, ensureDemo())
})
test.afterAll(async () => {
  await server?.close()
})

interface Watch { errors: string[]; gl: string[]; requests: string[] }
interface Opened { page: Page; w: Watch; close: () => Promise<void> }

async function open(browser: Browser, dev: Device, o: { reduced?: boolean; limits?: 'min' | 'mobile'; noWebgl2?: boolean; locale?: string } = {}): Promise<Opened> {
  const ctx = await browser.newContext({ ...DEVICES[dev], locale: o.locale ?? 'zh-CN', reducedMotion: o.reduced ? 'reduce' : 'no-preference' })
  const page = await ctx.newPage()
  const w: Watch = { errors: [], gl: [], requests: [] }
  page.on('pageerror', (e) => w.errors.push(`pageerror ${e.name}: ${e.message}`))
  page.on('console', (m) => {
    const t = m.text()
    if (/\[gpu-limits\]|GL_INVALID|GL ERROR|WebGL: /.test(t)) w.gl.push(t.split('\n')[0])
    else if (m.type() === 'error') w.errors.push(t.split('\n')[0])
  })
  page.on('request', (r) => w.requests.push(new URL(r.url()).pathname))
  if (o.limits) await page.addInitScript(emulate, { L: MIN, renderer: o.limits === 'mobile' ? ADRENO : null, mobile: o.limits === 'mobile' ? MOBILE : null })
  if (o.noWebgl2) {
    await page.addInitScript(() => {
      const gc = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, 'getContext')!.value as HTMLCanvasElement['getContext']
      HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, id: string, ...rest: unknown[]) {
        if (id === 'webgl2') return null
        return (gc as (this: HTMLCanvasElement, ...a: unknown[]) => unknown).call(this, id, ...rest)
      } as typeof gc
    })
  }
  await ctx.route('**/api/sandbox/v1/capacity', (r) => r.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify(CAPACITY()) }))
  await page.goto(`${server!.url}/`)
  await expect(page.locator('[data-view="landing"]')).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('[data-voxel-bg]')).toHaveAttribute('data-voxel-mode', /^(webgl2|canvas2d|static)$/, { timeout: 20_000 })
  return { page, w, close: () => ctx.close() }
}

const frames = (page: Page): Promise<number> =>
  page.evaluate(() => (document.querySelector('[data-voxel-bg]') as (HTMLCanvasElement & { voxelFrames?: number }) | null)?.voxelFrames ?? -1)

async function noOverflow(page: Page, where: string): Promise<void> {
  const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: innerWidth }))
  expect(m.sw, `horizontal overflow (${where})`).toBeLessThanOrEqual(m.iw)
}

/** visible links and buttons of the page smaller than 44 x 44 CSS px */
const smallTargets = (page: Page): Promise<string[]> => page.evaluate(() => [...document.querySelectorAll('[data-view="landing"] a, [data-view="landing"] button')]
  .filter((e) => {
    const r = e.getBoundingClientRect()
    const s = getComputedStyle(e)
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && (r.height < 44 || r.width < 44)
  })
  .map((e) => `${e.tagName} "${(e.textContent || e.getAttribute('aria-label') || '').trim().slice(0, 24)}" ${Math.round(e.getBoundingClientRect().width)}x${Math.round(e.getBoundingClientRect().height)}`))

for (const dev of Object.keys(DEVICES) as Device[]) {
  test(`landing on ${dev}: layout, actions, backdrop, console`, async ({ browser }) => {
    test.setTimeout(180_000)
    const { page, w, close } = await open(browser, dev)
    await expect(page.locator('[data-voxel-bg]')).toHaveAttribute('data-voxel-mode', 'webgl2')
    await expect(page.locator('[data-landing-capacity="open"]')).toBeVisible({ timeout: 20_000 })
    await expect(page.locator('h1')).toBeVisible()
    await expect(page.locator('[data-demo-launch]')).toHaveAttribute('href', '/world/synthcity')
    await page.locator('[data-demo-launch]').click({ trial: true })
    await expect(page.locator('[data-sandbox-open]')).toBeEnabled()
    await page.locator('[data-sandbox-open]').click({ trial: true })
    await expect(page.locator('[data-launch-hint]')).toBeVisible()
    await noOverflow(page, 'top')
    // every section is present and the page grows the backdrop while scrolling
    for (const id of ['capabilities', 'showcase', 'stack', 'open-source']) await expect(page.locator(`#${id}`)).toHaveCount(1)
    await expect(page.locator('[data-feature]')).toHaveCount(16)
    const f0 = await frames(page)
    await page.evaluate(() => scrollTo({ top: document.documentElement.scrollHeight, behavior: 'instant' }))
    await page.waitForTimeout(800)
    expect(await frames(page)).toBeGreaterThan(f0)
    await noOverflow(page, 'bottom')
    if (dev !== 'desktop') expect(await smallTargets(page), 'touch targets below 44 px').toEqual([])
    await page.waitForTimeout(500)
    expect(w.gl).toEqual([])
    expect(w.errors).toEqual([])
    await close()
  })
}

test('phone menu: Sheet with the sections, the language switch and both actions; a section link scrolls there', async ({ browser }) => {
  const { page, w, close } = await open(browser, 'iphone15')
  await expect(page.locator('[data-landing-nav]')).toBeHidden()
  await page.locator('[data-landing-menu]').tap()
  const sheet = page.locator('[data-landing-sheet]')
  await expect(sheet).toBeVisible()
  await expect(sheet.locator('[data-lang-switch]')).toBeVisible()
  await expect(sheet.locator('a[href="/world/synthcity"]')).toBeVisible()
  await expect(sheet.getByText(/开启我的沙盒/)).toBeVisible()
  await sheet.locator('a[href="#showcase"]').tap()
  await expect(sheet).toHaveCount(0, { timeout: 5000 })
  await expect.poll(() => page.evaluate(() => Math.round(document.getElementById('showcase')!.getBoundingClientRect().top)), { timeout: 5000 }).toBeLessThan(120)
  // language switch from the Sheet
  await page.locator('[data-landing-menu]').tap()
  await sheet.locator('[data-lang-switch] [lang="en"]').tap()
  await expect(page.locator('h1')).toHaveText(/Real-world 4D simulation/)
  expect(w.errors).toEqual([])
  await close()
})

test('first screen: no application chunk; the backdrop renderer is a lazy chunk', async ({ browser }) => {
  const { page, w, close } = await open(browser, 'desktop')
  await page.waitForTimeout(1000)
  const js = w.requests.filter((p) => p.endsWith('.js')).map((p) => p.split('/').pop()!.replace(/-[A-Za-z0-9_-]{8}\.js$/, ''))
  expect(js.filter((n) => ['three', 'ui', 'engine', 'main', 'r3f', 'state', 'net'].includes(n))).toEqual([])
  expect(js).toContain('voxelField')
  expect(w.errors).toEqual([])
  await close()
})

for (const [dev, set] of [['desktop', 'min'], ['iphone15', 'mobile'], ['ipad11l', 'mobile']] as const) {
  test(`WebGL2 specification minimum (${set}) on ${dev}: no WebGL error`, async ({ browser }) => {
    const { page, w, close } = await open(browser, dev, { limits: set })
    await expect(page.locator('[data-voxel-bg]')).toHaveAttribute('data-voxel-mode', 'webgl2')
    await page.evaluate(() => scrollTo({ top: 1500, behavior: 'instant' }))
    await page.waitForTimeout(1500)
    const s = await page.evaluate(() => (window as unknown as { __gpuLimits: { programs: number; maxV: number; maxF: number; maxBlockSize: number } }).__gpuLimits)
    expect(s.programs).toBeGreaterThanOrEqual(1)
    expect(s.maxV + s.maxF).toBe(0)
    expect(w.gl).toEqual([])
    expect(w.errors).toEqual([])
    await close()
  })
}

test('reduced motion: one still frame, sections shown at once', async ({ browser }) => {
  const { page, w, close } = await open(browser, 'pixel8', { reduced: true })
  const bg = page.locator('[data-voxel-bg]')
  await expect(bg).toHaveAttribute('data-voxel-state', 'still')
  const f0 = await frames(page)
  expect(f0).toBeGreaterThanOrEqual(1)
  await page.evaluate(() => scrollTo({ top: 3000, behavior: 'instant' }))
  await page.mouse.move(100, 100)
  await page.waitForTimeout(1500)
  expect(await frames(page)).toBe(f0)
  await expect(bg).toHaveAttribute('data-voxel-state', 'still')
  const hidden = await page.evaluate(() => [...document.querySelectorAll('.landing-reveal')].filter((e) => getComputedStyle(e).opacity !== '1').length)
  expect(hidden).toBe(0)
  expect(w.errors).toEqual([])
  await close()
})

test('without WebGL2 the Canvas2D renderer runs', async ({ browser }) => {
  const { page, w, close } = await open(browser, 'iphone15', { noWebgl2: true })
  await expect(page.locator('[data-voxel-bg]')).toHaveAttribute('data-voxel-mode', 'canvas2d')
  const f0 = await frames(page)
  await page.evaluate(() => scrollTo({ top: 1200, behavior: 'instant' }))
  await page.waitForTimeout(800)
  expect(await frames(page)).toBeGreaterThan(f0)
  await noOverflow(page, 'canvas2d')
  expect(w.errors).toEqual([])
  await close()
})

test('the loop pauses on a hidden page and when idle, and resumes on scroll', async ({ browser }) => {
  test.setTimeout(120_000)
  const { page, w, close } = await open(browser, 'desktop')
  const bg = page.locator('[data-voxel-bg]')
  await expect(bg).toHaveAttribute('data-voxel-state', 'running')
  // hidden page
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(bg).toHaveAttribute('data-voxel-state', 'paused')
  const fh = await frames(page)
  await page.waitForTimeout(800)
  expect(await frames(page)).toBe(fh)
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(bg).toHaveAttribute('data-voxel-state', 'running')
  // idle after 9 s without input
  await expect(bg).toHaveAttribute('data-voxel-state', 'idle', { timeout: 20_000 })
  const fi = await frames(page)
  await page.waitForTimeout(800)
  expect(await frames(page)).toBe(fi)
  await page.mouse.wheel(0, 600)
  await expect(bg).toHaveAttribute('data-voxel-state', 'running')
  await expect.poll(() => frames(page)).toBeGreaterThan(fi)
  expect(w.errors).toEqual([])
  await close()
})
