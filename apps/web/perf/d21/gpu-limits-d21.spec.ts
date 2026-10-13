// The D2.1 pages under the WebGL2 specification minimum (WP-13b, M16; D2-AC-54 for the D2.1 surfaces, D2-AC-49 shells; ADR-086,
// ADR-087; AWR-04 §15.8.6, §15.11.6). perf/m06/gpu-limits.spec.ts (M06) covers the field renderer on a bare canvas; this spec
// runs the full D2.1 user interface on the same emulation: the boot screen and the reveal, the field-view toolbar driven
// through the UI (the four views with data, the volumes, none), the power overlay (saving renders on demand, the first drag
// resumes), the settings page with its folded identity section, the phone shell (five windows, the layers drawer with the
// field view) and the tablet commander (docked panels, the field menu of the top bar); for the test build and for the public
// demo build (no test hooks: the UI alone; its power thresholds come from a routed capacity answer, as the server would send).
// Limit sets: `min` (MAX_UNIFORM_BUFFER_BINDINGS 24, 12 / 12 / 24 uniform blocks, MAX_UNIFORM_BLOCK_SIZE 16 384) on a desktop
// viewport, and `mobile` (the same plus ALIASED_POINT_SIZE_RANGE [1, 1], MAX_TEXTURE_SIZE 4096, no EXT_color_buffer_float,
// EXT_float_blend or OES_texture_float_linear) with an Adreno renderer string (deviceClass mobile) on phone and tablet
// viewports. Server: the M06 test server (FakeSource, /worlds from the repository).
// Asserts: no WebGL error or [gpu-limits] refusal, no three warning or error, no page error; every program within the
// per-stage and combined block limits and the block size; the mobile set makes the points quads; with the test build no
// program is compiled after the reveal (switching views, the overlay and the shells only change data).
import { expect, test, type Browser, type Page } from '@playwright/test'
import { startM06Server, type M06Server } from '../m06/server'
import { ensureBuild, frames, isTestBuild } from './live21'
import { ADRENO, emulate, MIN, MOBILE, VIEW, type Shell } from './limits'

const servers: Partial<Record<'test' | 'demo', M06Server>> = {}
/** FakeSource as in perf/m06/gpu-limits.spec.ts (vehicles on the ground ring, clear weather) */
const FAKE = 'source=fake&fakeWorld=synthcity&fakeN=6&fakeGround=1'

test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => {
  test.setTimeout(600_000)
  servers.test = await startM06Server(undefined, ensureBuild('test'))
  servers.demo = await startM06Server(undefined, ensureBuild('demo'))
})
test.afterAll(async () => {
  for (const s of Object.values(servers)) await s?.close()
})

interface Console { gl: string[]; three: string[]; errors: string[] }
function watchConsole(page: Page): Console {
  const c: Console = { gl: [], three: [], errors: [] }
  page.on('pageerror', (e) => c.errors.push(`${e.name}: ${e.message}`))
  page.on('console', (m) => {
    const t = m.text()
    if (/\[gpu-limits\]|GL_INVALID|GL ERROR|WebGL: /.test(t)) c.gl.push(t.split('\n')[0])
    else if ((m.type() === 'warning' || m.type() === 'error') && /THREE\./.test(t)) c.three.push(t.split('\n')[0])
    else if (m.type() === 'error' && /M15-E0\d\d|TypeError|ReferenceError|RangeError/.test(t)) c.errors.push(t.split('\n')[0])
  })
  return c
}

type Hooks = { __perf?: { gpu: { programs: number } }; __gpuLimits: { programs: number; maxV: number; maxF: number; maxBlockSize: number }
  __vp?: { quality(): { pointSizeMode: string; formFactor: string }; field: { view(k: string, o?: { volumes?: string }): Promise<string>; targets(n: number): Promise<string[]>; stats(): { points: number } | null } }
  __ux?: { power: { state: string } } }

const CAPACITY = (idle: number, warn: number): Record<string, unknown> => ({
  schema: 'awr.capacity.v1', t_unix_ns: String(BigInt(Date.now()) * 1_000_000n),
  build: { version: '0.2.0', channel: 'demo', commit: 'abcdef0123', dirty: false, built_at: '2026-10-06T00:00:00Z', release: '20261006-000000-abcdef0123' },
  accepting: true, maintenance: false, degrade_level: 'L0',
  viewers: { live: 3, max: 24, queue: 0, eta_s: null, state: 'open' },
  sandbox: { active: 0, max: 2, reserved_internal: 1, internal_active: 0, parked: 0, queue: 0, eta_s: 0, admit_now: true, worlds_admit_now: ['synthcity'], state: 'open' },
  power: { viewer_idle_s: idle, sandbox_idle_s: 600, hidden_s: 60, warn_s: warn }, hint: 'open',
})

async function newPage(browser: Browser, shell: Shell, set: 'min' | 'mobile'): Promise<{ page: Page; c: Console; close: () => Promise<void> }> {
  const v = VIEW[shell]
  const ctx = await browser.newContext({ viewport: { width: v.width, height: v.height }, deviceScaleFactor: v.dpr, isMobile: shell === 'phone', hasTouch: v.touch, locale: 'zh-CN' })
  const page = await ctx.newPage()
  const c = watchConsole(page)
  await page.addInitScript(emulate, { L: MIN, renderer: set === 'mobile' ? ADRENO : null, mobile: set === 'mobile' ? MOBILE : null })
  await page.addInitScript(() => localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 })))
  await ctx.route('**/api/sandbox/v1/capacity', (r) => r.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify(CAPACITY(45, 3)) }))
  return { page, c, close: () => ctx.close() }
}

async function revealCanvas(page: Page): Promise<void> {
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 240_000 })
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  await frames(page, 20)
}

/** the field toolbar of the shell: desktop bar, phone layers drawer, tablet field menu */
async function fieldViews(page: Page, shell: Shell, withData: boolean): Promise<void> {
  if (withData) await page.evaluate(() => (window as unknown as Hooks).__vp!.field.targets(2))
  const pick = async (k: string): Promise<void> => {
    if (shell === 'desktop') await page.locator(`[data-figure="field-bar"] [data-field-option="${k}"]`).click()
    else if (shell === 'phone') {
      if (!(await page.locator('[data-layers-sheet]').count())) await page.locator('[data-phone-layers]').tap()
      await page.locator(`[data-layers-sheet] [data-field-option="${k}"]`).tap()
    } else {
      if (!(await page.locator('[data-field-popover]').count())) await page.locator('[data-field-menu]').tap()
      await page.locator(`[data-field-popover] [data-field-option="${k}"]`).tap()
    }
    // the test build gets the profile the server would send right after the subscription (M06 test hook)
    if (withData && k !== 'none') await page.evaluate((kk) => (window as unknown as Hooks).__vp!.field.view(kk, { volumes: 'all' }), k)
    await frames(page, 12)
  }
  for (const k of ['acoustic', 'visual', 'thermal', 'radar']) {
    await pick(k)
    // the renderer rebuilds on data at most every 0.9 s (WP-11b): wait for the build of this view's profile
    if (withData) {
      await expect.poll(async () => (await page.evaluate(() => (window as unknown as Hooks).__vp!.field.stats()))?.points ?? 0,
        { message: k, timeout: 10_000 }).toBeGreaterThan(0)
    }
  }
  await pick('none')
  if (shell !== 'desktop') await page.keyboard.press('Escape')
}

/** power save and resume: the overlay renders on demand, the first drag resumes */
async function powerCycle(page: Page, shell: Shell): Promise<void> {
  await page.mouse.move(VIEW[shell].width / 2, VIEW[shell].height / 2)
  await page.mouse.move(VIEW[shell].width / 2 + 60, VIEW[shell].height / 2 + 20, { steps: 4 })
  await expect(page.locator('[data-power-overlay="SAVING"]')).toBeVisible({ timeout: 70_000 })
  await page.waitForTimeout(1500)
  await page.setViewportSize({ width: VIEW[shell].width, height: VIEW[shell].height - 40 })   // a resize renders one frame
  await page.waitForTimeout(500)
  await page.setViewportSize({ width: VIEW[shell].width, height: VIEW[shell].height })
  const x = VIEW[shell].width / 2
  const y = VIEW[shell].height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.mouse.move(x + 50, y + 15, { steps: 5 })
  await page.mouse.up()
  await expect(page.locator('[data-power-overlay]')).toHaveCount(0, { timeout: 30_000 })
  await frames(page, 20)
}

async function verdict(page: Page, c: Console, programsAtReveal: number | null): Promise<void> {
  const end = await page.evaluate(() => {
    const g = window as unknown as Hooks
    const cv = document.querySelector<HTMLCanvasElement>('[data-viewport] canvas')
    const gl = cv?.getContext('webgl2') ?? null
    return { emulated: g.__gpuLimits, programs: g.__perf?.gpu.programs ?? null, glError: gl ? gl.getError() : 0 }
  })
  test.info().annotations.push({ type: 'gpu-limits-d21', description: JSON.stringify({ ...end, programsAtReveal }) })
  expect(end.glError).toBe(0)
  expect(end.emulated.programs).toBeGreaterThan(3)
  expect(end.emulated.maxV).toBeLessThanOrEqual(MIN.vertex)
  expect(end.emulated.maxF).toBeLessThanOrEqual(MIN.fragment)
  expect(end.emulated.maxV + end.emulated.maxF).toBeLessThanOrEqual(MIN.combined)
  expect(end.emulated.maxBlockSize).toBeLessThanOrEqual(MIN.blockSize)
  if (programsAtReveal !== null && end.programs !== null) expect(end.programs - programsAtReveal).toBeLessThanOrEqual(0)
  expect(c.gl, c.gl.join('\n')).toEqual([])
  expect(c.three, c.three.join('\n')).toEqual([])
  expect(c.errors, c.errors.join('\n')).toEqual([])
}

const CASES: { build: 'test' | 'demo'; shell: Shell; set: 'min' | 'mobile' }[] = [
  { build: 'test', shell: 'desktop', set: 'min' },
  { build: 'test', shell: 'phone', set: 'mobile' },
  { build: 'test', shell: 'tablet', set: 'mobile' },
  { build: 'demo', shell: 'desktop', set: 'min' },
  { build: 'demo', shell: 'phone', set: 'mobile' },
]

for (const k of CASES) {
  test(`D2.1 pages under the minimum limits: ${k.build} build, ${k.shell}, limits ${k.set}`, async ({ browser }) => {
    test.setTimeout(600_000)
    const { page, c, close } = await newPage(browser, k.shell, k.set)
    const base = servers[k.build]!.url
    // the idle clock runs from the page load, not the reveal (M15 defect, power-boot.spec.ts): thresholds beyond the boot time
    const q = k.build === 'test' ? '&power=viewer&powerIdle=45&powerWarn=3&powerQuiet=30' : ''
    await page.goto(`${base}/world/synthcity?${FAKE}${q}`)
    await expect(page.locator('#boot-mask .boot-version')).toBeVisible({ timeout: 30_000 })
    await revealCanvas(page)
    const hooks = await isTestBuild(page)
    expect(hooks).toBe(k.build === 'test')
    const programsAtReveal = hooks ? await page.evaluate(() => (window as unknown as Hooks).__perf?.gpu.programs ?? null) : null
    if (hooks) {
      const qv = await page.evaluate(() => (window as unknown as Hooks).__vp!.quality())
      expect(qv.pointSizeMode).toBe(k.set === 'mobile' ? 'quad' : 'glpoint')
      expect(qv.formFactor).toBe(k.shell)
    }
    if (k.shell !== 'desktop') await expect(page.locator(`[data-shell="${k.shell}"]`)).toBeAttached()
    await fieldViews(page, k.shell, hooks)
    if (k.shell === 'phone') {
      for (const tab of ['fleet', 'targets', 'alerts', 'viewport']) await page.locator(`[data-tab="${tab}"]`).tap()
      await frames(page, 10)
    }
    await powerCycle(page, k.shell)
    await verdict(page, c, programsAtReveal)
    // the settings page with the folded identity section (no backend: nothing is submitted)
    if (k.build === 'test') {
      await page.goto(`${base}/world/synthcity?${FAKE}&settings=general`)
      await revealCanvas(page)
      await page.locator('[data-advanced-toggle]').click()
      await expect(page.locator('[data-identity-input]')).toBeVisible()
      await frames(page, 10)
      await page.keyboard.press('Escape')
      await frames(page, 10)
      await verdict(page, c, null)
    }
    await close()
  })
}

test('demo build landing page and boot screen under the minimum limits', async ({ browser }) => {
  test.setTimeout(300_000)
  const { page, c, close } = await newPage(browser, 'desktop', 'min')
  await page.goto(`${servers.demo!.url}/`)
  await expect(page.locator('[data-view="landing"]')).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('[data-landing-capacity="open"]')).toBeVisible({ timeout: 20_000 })
  await page.waitForTimeout(1500)
  expect(c.gl).toEqual([])
  expect(c.errors).toEqual([])
  await close()
})
