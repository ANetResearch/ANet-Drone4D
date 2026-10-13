// M15 D2.1 functional specs, desktop shell (WP-12c; M15-AC-220 to AC-223, AC-227 to AC-229; D2-AC-40, 41, 43, 44, 51 to 53
// functional parts). Test build with a `ci` channel and a fixed release (build: AWR_BUILD_CHANNEL=ci AWR_BUILD_SHA=abcdef0123
// AWR_RELEASE=20261005-000000-abcdef0123 M15_DIST=<dir> node perf/m15/helpers/build-ui.mjs), FakeSource, no backend: the public
// capacity, the identity and viewer-queue endpoints and the sandbox REST are page routes. Functional only (no timing
// thresholds; the D2-AC performance parts belong to the M16 harness).
//   1. version line: static text visible with JavaScript disabled, equal to build-info.json and __AWR_BUILD__; five segments
//      whose widths follow the weights (+-2 px); the capacity line for open, viewer full, sandbox full and failing answers,
//      the mask cadence >= 5 s; "site updated" banner for another release, once, no automatic reload;
//   2. power save (viewer, test thresholds): countdown, any input cancels, 8 px jitter does not, saving closes the link and
//      suspends the loop (no frames, no requests), the overlay leaves the first drag to the camera and resumes to LIVE;
//      seats full (4431) -> viewer queue card -> seat_ready -> connected again;
//   3. identity: folded "advanced" section, wrong / locked / ok, the badge and the restored entries, the pass phrase (generated
//      by the test at run time) nowhere in storage, the page text or the console, logout;
//   4. field controls and legend: five views, volumes, physical reference, Shift+V, legend bands and footnote;
//   5. sandbox surface: the readout card on the target detail page (rows, sentence, probability note), park and resume.
// Run: `M15_DIST=<dir> npx playwright test perf/m15/d21-shell.spec.ts --project perf` (AWR_PORT_OFFSET for a private port).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, DIST, glyphScan, isTestBuild, shot, startPreview, watch } from './helpers/shell'
import { open as openSandbox, routes as sandboxRoutes, SID, storeSession, TOKEN } from './helpers/d2rest'

const PORT = 4191
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 } })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})
test.describe.configure({ mode: 'serial' })

const BUILD = JSON.parse(readFileSync(join(DIST.startsWith('/') ? DIST : join(import.meta.dirname, '..', '..', DIST), 'build-info.json'), 'utf8')) as {
  version: string; channel: string; commit: string; dirty: boolean; built_at: string; release: string
}
const display = `V${BUILD.version}-${BUILD.channel} · ${BUILD.commit === 'nogit' ? 'nogit' : BUILD.commit.slice(0, 7)}${BUILD.dirty ? '-dirty' : ''} · ${BUILD.built_at.slice(0, 10)}`

function capacity(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema: 'awr.capacity.v1', t_unix_ns: String(BigInt(Date.now()) * 1_000_000n), build: BUILD, accepting: true, maintenance: false, degrade_level: 'L0',
    viewers: { live: 18, max: 24, queue: 0, eta_s: null, state: 'open' },
    sandbox: { active: 1, max: 2, reserved_internal: 1, parked: 0, queue: 0, eta_s: null, admit_now: true, worlds_admit_now: ['synthcity'], state: 'open' },
    power: { viewer_idle_s: 300, sandbox_idle_s: 600, hidden_s: 60, warn_s: 30 }, hint: 'open', ...over,
  }
}

type Ux = { __ux: { power: { state: string; transitions: string[]; presence: number }; boot: { progress: { p: number }[]; capacity: string }; identity: { tier: string }; rtCalls: string[] }
  __uxInject: { power: { conn(s: string, c: number | null): void; tick(): void; state(): string }; quant(f: unknown, select?: boolean): void }
  __vp?: { loop: { stats(): { frameNo: number; suspended: boolean; suspendedFrames: number } }; field?: { view(kind: string, o?: Record<string, unknown>): Promise<string> } } }

async function testBuild(page: Page): Promise<void> {
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 60_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
}

test('version line, five segments and the boot capacity line', async ({ page, context, browser }) => {
  // 1a. with JavaScript disabled the static line is there (D2-AC-40)
  const nojs = await browser.newContext({ javaScriptEnabled: false, baseURL: baseUrl(PORT) })
  const p0 = await nojs.newPage()
  await p0.goto('/world/shenzhen')
  await expect(p0.locator('#boot-mask .boot-version')).toHaveText(display)
  expect(await p0.locator('meta[name="awr-build"]').getAttribute('content')).toBe(`${BUILD.version}-${BUILD.channel}+${BUILD.commit}${BUILD.dirty ? '-dirty' : ''}`)
  // 1b. segment widths by weight (bundle 15, world 10, points 40, warming 30, reveal 5; 2 px gaps)
  const widths = await p0.locator('#boot-mask [data-seg]').evaluateAll((els) => els.map((e) => e.getBoundingClientRect().width))
  expect(widths).toHaveLength(5)
  const total = widths.reduce((a, b) => a + b, 0)
  const want = [0.15, 0.1, 0.4, 0.3, 0.05].map((w) => w * total)
  for (let i = 0; i < 5; i++) expect(Math.abs(widths[i] - want[i])).toBeLessThanOrEqual(2)
  await shot(p0, 'm15-d21-boot-nojs')
  await nojs.close()

  // 1c. the capacity line while the mask is up (no ?reveal=shell: the mask waits for gates the UI-only run never resolves)
  const w = watch(page)
  const hits: number[] = []
  let answer: Record<string, unknown> | null = capacity()
  await context.route('**/api/sandbox/v1/capacity', (r) => {
    hits.push(Date.now())
    return answer ? r.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify(answer) }) : r.fulfill({ status: 503, body: '{}' })
  })
  await page.goto('/world/shenzhen?source=fake&fakeN=2&viewport=off')
  const line = page.locator('#boot-mask .boot-capacity')
  await expect(line).toHaveAttribute('data-capacity-hint', 'open', { timeout: 15_000 })
  await expect(line).toHaveText('实时观看 18 / 24 · 沙盒 1 / 2 可用')
  expect(await page.evaluate(() => (window as unknown as { __AWR_BUILD__?: unknown }).__AWR_BUILD__ ?? null)).toBeNull() // a define, not a global
  answer = capacity({ viewers: { live: 24, max: 24, queue: 2, eta_s: 150, state: 'full' }, hint: 'queue_viewer' })
  await expect(line).toHaveAttribute('data-capacity-hint', 'queueViewer', { timeout: 12_000 })
  await expect(line.locator('.boot-capacity-text')).toHaveText('实时观看已满（24 / 24）· 进入后排队，第 3 位，约 3 min')
  await expect(line.locator('[data-capacity-queue]')).toHaveText('需排队')
  await shot(page, 'm15-d21-boot-queue')
  answer = capacity({ sandbox: { active: 2, max: 2, reserved_internal: 1, parked: 3, queue: 3, eta_s: 360, admit_now: false, worlds_admit_now: [], state: 'queue' }, hint: 'queue_sandbox' })
  await expect(line).toHaveAttribute('data-capacity-hint', 'queueSandbox', { timeout: 12_000 })
  await expect(line.locator('.boot-capacity-text')).toHaveText('沙盒 2 / 2 在用 · 排队 3 人 · 预计约 6 min')
  // the cadence while the mask is up: every request at least 5 s after the previous one (the first is immediate)
  for (let i = 1; i < hits.length; i++) expect(hits[i] - hits[i - 1]).toBeGreaterThanOrEqual(4900)
  expect(hits.length).toBeGreaterThanOrEqual(3)
  expect(w.errors).toEqual([])
})

test('a failing capacity endpoint never delays the reveal; another release shows "site updated" once', async ({ page, context }) => {
  const w = watch(page)
  await context.route('**/api/sandbox/v1/capacity', (r) => r.fulfill({ status: 200, headers: { 'content-type': 'application/json' },
    body: JSON.stringify(capacity({ build: { ...BUILD, release: '20261006-000000-0123456789' } })) }))
  await page.goto('/world/shenzhen?source=fake&fakeN=2&reveal=shell&viewport=off')
  await testBuild(page)
  const banner = page.locator('[data-update-banner]')
  await expect(banner).toBeVisible({ timeout: 10_000 })
  await expect(banner).toContainText('站点已更新')
  await shot(page, 'm15-d21-update-banner')
  await banner.getByRole('button', { name: '关闭' }).click()
  await expect(banner).toHaveCount(0)
  // the about tab shows the same build line and copies the diagnostics
  await page.goto('/world/shenzhen?source=fake&fakeN=2&reveal=shell&viewport=off&settings=about')
  await testBuild(page)
  await expect(page.locator('[data-about-build]')).toHaveText(display)
  await expect(page.locator('[data-update-banner]')).toHaveCount(0) // once per release (sessionStorage)
  expect(w.errors).toEqual([])

  const p2 = await context.newPage()
  await context.unroute('**/api/sandbox/v1/capacity')
  await context.route('**/api/sandbox/v1/capacity', (r) => r.abort())
  const t0 = Date.now()
  await p2.goto('/world/shenzhen?source=fake&fakeN=2&reveal=shell&viewport=off')
  await expect(p2.locator('#boot-mask')).toHaveCount(0, { timeout: 60_000 })
  expect(Date.now() - t0).toBeLessThan(30_000)
  await p2.close()
})

const POWER_Q = 'source=fake&fakeN=3&reveal=shell&power=viewer&powerIdle=14&powerWarn=5&powerQuiet=6&powerHidden=4'

/** test builds: count the setInterval callbacks that run while `window.__ivOn` is set (round 2: the periodic UI work of
 *  the shell registers through lib/powerFreeze and stops while saving; d2-FX-R2-sandbox-to-M15 item 1) */
async function countIntervals(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __ivN: number; __ivOn: boolean }
    w.__ivN = 0
    w.__ivOn = false
    const orig = window.setInterval.bind(window) as (fn: TimerHandler, d?: number, ...a: unknown[]) => number
    ;(window as unknown as { setInterval: unknown }).setInterval = (fn: TimerHandler, d?: number, ...a: unknown[]) =>
      orig((...x: unknown[]) => {
        if (w.__ivOn) w.__ivN++
        if (typeof fn === 'function') (fn as (...y: unknown[]) => void)(...x)
      }, d, ...a)
  })
}

test('power save of a viewer: countdown, cancel, saving without frames or requests, resume on the first drag', async ({ page }) => {
  const w = watch(page)
  await countIntervals(page)
  await page.goto(`/world/shenzhen?${POWER_Q}`)
  await testBuild(page)
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  const state = () => page.evaluate(() => (window as unknown as Ux).__ux.power.state)
  // a real move (>= 8 px) restarts the idle clock; the countdown then starts 9 s later and saving 14 s later
  await page.mouse.move(600, 400)
  await page.mouse.move(800, 450, { steps: 5 })
  // countdown at idle - warn (9 s); a 3 px jitter keeps it, a real move cancels it
  const banner = page.locator('[data-power-banner]')
  await expect(banner).toBeVisible({ timeout: 15_000 })
  await expect(banner).toContainText('s 后进入节能模式')
  await page.mouse.move(803, 450)
  await page.waitForTimeout(1200)
  await expect(banner).toBeVisible()
  await shot(page, 'm15-d21-power-warning')
  await page.mouse.move(860, 470, { steps: 4 })
  await expect(banner).toHaveCount(0)
  expect(await state()).toBe('ACTIVE')
  // saving at the threshold: overlay, loop suspended, no frames and no requests for 3 s
  await expect(page.locator('[data-power-overlay="SAVING"]')).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('[data-power-role]')).toHaveText('共享展示在服务器上继续运行，任意操作恢复')
  await page.waitForTimeout(600)
  const s0 = await page.evaluate(() => (window as unknown as Ux).__vp?.loop.stats() ?? null)
  expect(s0?.suspended).toBe(true)
  const requests: string[] = []
  page.on('request', (r) => requests.push(r.url()))
  await page.evaluate(() => { (window as unknown as { __ivOn: boolean }).__ivOn = true })
  await page.waitForTimeout(3000)
  const s1 = await page.evaluate(() => (window as unknown as Ux).__vp?.loop.stats() ?? null)
  const intervals = await page.evaluate(() => { const x = window as unknown as { __ivOn: boolean; __ivN: number }; x.__ivOn = false; return x.__ivN })
  expect((s1?.frameNo ?? 0) - (s0?.frameNo ?? 0)).toBeLessThanOrEqual(1)
  expect(requests.filter((u) => !u.includes('/assets/') && !u.startsWith('data:'))).toEqual([])
  // no periodic shell work while saving: the 1 Hz controller check, clocks, URL sync, store-write roller, renew check, 4 Hz
  // arbitration and event flush are all frozen (setTimeout chains of other modules are not counted here)
  expect(intervals).toBe(0)
  // the presented-interval figure measures the pause: "paused", never the red (d2-WP-13b-to-M15 item 5)
  const hud = page.locator('[data-figure="perf-hud"]')
  if (await hud.count()) {
    await expect(hud.locator('[data-perf-paused]')).toBeVisible()
    await expect(hud.locator('.text-lf-hero-text')).toHaveCount(0)
  }
  await shot(page, 'm15-d21-power-saving')
  // the first drag rotates the camera and resumes; the link comes back LIVE
  await page.mouse.move(700, 400)
  await page.mouse.down()
  await page.mouse.move(760, 420, { steps: 6 })
  await page.mouse.up()
  await expect.poll(state, { timeout: 20_000 }).toBe('ACTIVE')
  await expect(page.locator('[data-power-overlay]')).toHaveCount(0)
  if (await hud.count()) await expect(hud.locator('[data-perf-paused]')).toHaveCount(0, { timeout: 10_000 })
  const tr = await page.evaluate(() => (window as unknown as Ux).__ux.power.transitions)
  expect(tr.some((x) => x.startsWith('ACTIVE>WARNING') || x.startsWith('QUIET>WARNING'))).toBe(true)
  expect(tr).toContain('WARNING>SAVING:idle')
  expect(tr.some((x) => x.startsWith('SAVING>RESUMING'))).toBe(true)
  expect(tr.some((x) => x.startsWith('RESUMING>ACTIVE'))).toBe(true)
  // the server's reclaim (4430) saves at once without a countdown
  await page.evaluate(() => (window as unknown as Ux).__uxInject.power.conn('RECONNECTING', 4430))
  await expect(page.locator('[data-power-overlay="SAVING"]')).toBeVisible()
  await page.keyboard.press('KeyX')
  await expect.poll(state, { timeout: 20_000 }).toBe('ACTIVE')
  expect(w.errors).toEqual([])
})

test('live viewer seats full: viewer queue card, seat_ready connects again', async ({ page, context }) => {
  const w = watch(page)
  let polls = 0
  const queued: string[] = []
  await context.route('**/api/viewer/queue**', (r) => {
    const req = r.request()
    queued.push(`${req.method()} ${new URL(req.url()).pathname}`)
    if (req.method() === 'POST') return r.fulfill({ status: 201, headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ticket_id: 'vq-abcdefghij', ticket_token: 'vq1.test', position: 1, eta_s: 90 }) })
    if (req.method() === 'DELETE') return r.fulfill({ status: 204, body: '' })
    polls++
    return r.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify(polls < 2
      ? { ticket_id: 'vq-abcdefghij', state: 'waiting', position: 1, eta_s: 60 } : { ticket_id: 'vq-abcdefghij', state: 'seat_ready', position: 0, eta_s: 0 }) })
  })
  await page.goto(`/world/shenzhen?source=fake&fakeN=2&reveal=shell&viewport=off&power=viewer`)
  await testBuild(page)
  await page.evaluate(() => (window as unknown as Ux).__uxInject.power.conn('RECONNECTING', 4431))
  const card = page.locator('[data-queue-kind="viewer"]')
  await expect(card).toBeVisible()
  await expect(card).toContainText('实时观看排队中 · 第 1 位')
  await shot(page, 'm15-d21-viewer-queue')
  await expect.poll(() => page.evaluate(() => (window as unknown as Ux).__ux.power.state), { timeout: 20_000 }).toBe('ACTIVE')
  await expect(card).toHaveCount(0)
  expect(queued[0]).toBe('POST /api/viewer/queue')
  expect(queued.some((x) => x.startsWith('GET /api/viewer/queue/vq-abcdefghij'))).toBe(true)
  expect(w.errors).toEqual([])
})

test('internal identity: folded entry, wrong, locked, ok, restored entries, the pass phrase nowhere, logout', async ({ page, context }) => {
  const w = watch(page)
  // generated at run time (the deployment's pass phrase is only in the server's secret file)
  const secret = `t-${Date.now().toString(36)}-${Math.floor(performance.now() * 1000).toString(36)}`
  const bodies: string[] = []
  let mode: 'wrong' | 'locked' | 'ok' = 'wrong'
  await context.route('**/api/auth/identity', (r) => {
    const req = r.request()
    if (req.method() === 'DELETE') return r.fulfill({ status: 204, body: '' })
    bodies.push(req.postData() ?? '')
    if (mode === 'wrong') return r.fulfill({ status: 401, headers: { 'content-type': 'application/problem+json' }, body: JSON.stringify({ code: 304, message: 'AUTH_FAILED' }) })
    if (mode === 'locked') return r.fulfill({ status: 429, headers: { 'content-type': 'application/problem+json', 'retry-after': '1800' }, body: JSON.stringify({ code: 516, detail: { scope: 'prefix', retry_after_s: 1800 } }) })
    return r.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify({ identity_token: 'v1.eyJ2IjoxLCJyb2xlIjoiaW50ZXJuYWwifQ.c2ln', exp_unix_ns: String(BigInt(Date.now() + 43_200_000) * 1_000_000n), tier: 'internal' }) })
  })
  await page.goto('/world/shenzhen?source=fake&fakeN=2&reveal=shell&viewport=off&settings=general')
  await testBuild(page)
  const adv = page.locator('[data-settings-advanced]')
  await expect(adv).toBeVisible()
  await expect(page.locator('[data-identity-input]')).toHaveCount(0) // folded by default
  await page.locator('[data-advanced-toggle]').click()
  const input = page.locator('[data-identity-input]')
  await input.fill(secret)
  await page.locator('[data-identity-verify]').click()
  await expect(input).toHaveValue('') // cleared before the request left
  await expect(page.locator('[data-identity-error="wrong"]')).toHaveText('口令不正确')
  mode = 'locked'
  await input.fill(secret)
  await page.locator('[data-identity-verify]').click()
  await expect(page.locator('[data-identity-error="locked"]')).toHaveText('尝试次数过多，请 30 分钟后再试')
  await expect(page.locator('[data-identity-verify]')).toBeDisabled()
  await page.evaluate(() => sessionStorage.removeItem('awr.identity.v1'))
  // a fresh page (the lock is the server's; the client only waits for Retry-After)
  mode = 'ok'
  await page.reload()
  await testBuild(page)
  await page.locator('[data-advanced-toggle]').click()
  await page.locator('[data-identity-input]').fill(secret)
  await page.locator('[data-identity-verify]').click()
  await expect(page.locator('[data-identity-section="internal"] [data-identity-badge]')).toHaveText('内部 · 全功能')
  await shot(page, 'm15-d21-identity-ok')
  expect(bodies.every((b) => JSON.parse(b).secret === secret)).toBe(true)
  expect(await page.evaluate(() => (window as unknown as Ux).__ux.identity.tier)).toBe('internal')
  // the pass phrase is in no storage, no page text and no console line
  const dump = await page.evaluate(() => {
    const all: string[] = []
    for (const st of [localStorage, sessionStorage]) for (let i = 0; i < st.length; i++) all.push(`${st.key(i)}=${st.getItem(st.key(i) ?? '')}`)
    return { storage: all.join('\n'), text: document.body.innerText, html: document.documentElement.outerHTML }
  })
  expect(dump.storage).not.toContain(secret)
  expect(dump.text).not.toContain(secret)
  expect(dump.html).not.toContain(secret)
  expect(w.console.join('\n')).not.toContain(secret)
  // the header badge and the restored entries (the local build shows them anyway; the badge is the identity's)
  await page.keyboard.press('Escape')
  await expect(page.locator('header [data-identity-badge]')).toBeVisible()
  // no palette action, hotkey or help row for the identity section
  await page.keyboard.press('Control+KeyK')
  await page.keyboard.type('身份')
  await expect(page.locator('[data-slot="command-item"]').filter({ hasText: '口令' })).toHaveCount(0)
  await page.keyboard.press('Escape')
  // logout
  await page.goto('/world/shenzhen?source=fake&fakeN=2&reveal=shell&viewport=off&settings=general')
  await testBuild(page)
  await page.locator('[data-advanced-toggle]').click()
  await page.locator('[data-identity-logout]').click()
  await page.locator('[data-identity-logout-confirm]').click()
  await expect(page.locator('[data-identity-form]')).toBeVisible()
  expect(await page.evaluate(() => sessionStorage.getItem('awr.identity.v1'))).toBeNull()
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors).toEqual([])
})

test('field view controls, Shift+V and the legend', async ({ page }) => {
  const w = watch(page)
  await page.goto('/world/synthcity?source=fake&fakeWorld=synthcity&fakeN=4&reveal=shell')
  await testBuild(page)
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  const bar = page.locator('[data-figure="field-bar"]')
  await expect(bar).toBeVisible()
  await expect(page.locator('[data-field-legend]')).toHaveCount(0)
  await bar.locator('[data-field-option="acoustic"]').click()
  await expect(page.locator('[data-field-view]')).toHaveAttribute('data-field-view', 'acoustic')
  // synthetic profiles of the M06 demo hook fill the legend (unit, five bands, dashed reference shell, marks, footnote)
  await page.evaluate(() => (window as unknown as Ux).__vp?.field?.view('acoustic'))
  const legend = page.locator('[data-field-legend="acoustic"]')
  await expect(legend).toBeVisible()
  await expect(legend).toHaveAttribute('data-field-unit', 'dba', { timeout: 10_000 })
  await expect(legend.locator('[data-band]')).toHaveCount(5)
  await expect(legend.locator('[data-field-bands] span')).toHaveCount(5)
  await expect(legend).toContainText('模拟参考值 · 自由空间场')
  await expect(legend.locator('[data-field-marks]')).toContainText('圆 声学')
  await shot(page, 'm15-d21-field-acoustic')
  // Shift+V cycles acoustic -> visual -> thermal -> radar -> none
  await page.mouse.click(800, 300)
  for (const want of ['visual', 'thermal', 'radar', 'none']) {
    await page.keyboard.press('Shift+KeyV')
    await expect(page.locator('[data-field-view]')).toHaveAttribute('data-field-view', want)
  }
  await bar.locator('[data-volumes-option="all"]').click()
  await expect(page.locator('[data-field-volumes]')).toHaveAttribute('data-field-volumes', 'all')
  await bar.locator('[data-field-physical]').click()
  await expect(bar.locator('[data-field-physical]')).toHaveAttribute('data-field-physical', 'on')
  // the choice persists (awr.ui.prefs.v1)
  const prefs = await page.evaluate(() => JSON.parse(localStorage.getItem('awr.ui.prefs.v1') ?? '{}'))
  await page.waitForTimeout(700)
  const prefs2 = await page.evaluate(() => JSON.parse(localStorage.getItem('awr.ui.prefs.v1') ?? '{}'))
  expect((prefs2.data ?? prefs2).fieldVolumes ?? (prefs.data ?? prefs).fieldVolumes).toBe('all')
  expect(w.errors).toEqual([])
})

const QUANT = {
  t_sim_ns: 1, target: 'person-01', criterion_mode: 'geometric',
  pairs: [
    { uav: 'uav0001', model_id: 'p600_mid360', range_m: 182, elev_deg: 18, az_rel_deg: 12, los: true, los_age_s: 0.2,
      acoustic: { lp_dba: 31.4, lp_band_db: 30.9, band_hz: 150, thr_dba: 33.0, l_bg_dba: 30, margin_db: -2.1, p: 0.26, in_body: false },
      visual: { theta_arcmin: 8.2, d_vis_m: 0.436, n_eq_px: 8.2, theta_th_arcmin: 1.0, ratio: 8.2, c_app: 0.24, light_band: 'day', lights_on: false, in_fov: true, p: 1, in_body: false },
      reverse: null, p_any: 1, would_discover: false, dwell_s: 0, t_dwell_s: 2 },
    { uav: 'uav0002', model_id: 'p600_mid360', range_m: 410, elev_deg: 9, az_rel_deg: -40, los: true, los_age_s: 0.2,
      acoustic: { lp_dba: 24.1, lp_band_db: 23.5, band_hz: 150, thr_dba: 33.0, l_bg_dba: 30, margin_db: -9.5, p: 0.01, in_body: false },
      visual: { theta_arcmin: 3.7, d_vis_m: 0.436, n_eq_px: 3.7, theta_th_arcmin: 1.0, ratio: 3.7, c_app: 0.2, light_band: 'day', lights_on: false, in_fov: true, p: 0.98, in_body: false },
      reverse: null, p_any: 0.98, would_discover: false, dwell_s: 0, t_dwell_s: 2 },
  ],
}

test('sandbox: the perception readout card on the target detail page', async ({ page, context }) => {
  test.skip(process.env.M15_UI_ONLY === '1', 'needs the real viewport (M06)')
  const w = watch(page)
  const s = { posts: [] as { url: string; body: unknown; headers: Record<string, string> }[], preview: 'ok' as const }
  await sandboxRoutes(context, s)
  const future = () => String(BigInt(Date.now() + 1_800_000) * 1_000_000n)
  await context.route(`**/api/sandbox/v1/sessions/${SID}/park`, (r) => r.fulfill({ status: 202, headers: { 'content-type': 'application/json' }, body: JSON.stringify({ state: 'PARKING' }) }))
  await context.route(`**/api/sandbox/v1/sessions/${SID}/resume`, (r) => r.fulfill({ status: 201, headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ session: { ...JSON.parse(readFileSync(join(import.meta.dirname, '..', '..', '..', '..', 'packages', 'contracts', 'fixtures', 'd2', 'rest', 'session.json'), 'utf8')), state: 'READY', expires_unix_ns: future() }, token: TOKEN, token_exp_unix_ns: future() }) }))
  await storeSession(page)
  await openSandbox(page, true)
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  // the readout tab of a target
  await page.locator('[data-entity="targets"]').click()
  await expect(page.locator('[data-target-row]').first()).toBeVisible({ timeout: 20_000 })
  await page.locator('[data-target-row]').first().click()
  await expect(page.locator('[data-target-detail]')).toBeVisible()
  const id = await page.locator('[data-target-detail]').getAttribute('data-target-detail')
  await page.locator('[data-target-tab="quant"]').click()
  await page.evaluate((f) => (window as unknown as Ux).__uxInject.quant(f), { ...QUANT, target: id })
  const card = page.locator('[data-quant-card="2"]')
  await expect(card).toBeVisible({ timeout: 10_000 })
  await expect(card).toContainText('30.9 / 33.0')
  await expect(card).toContainText('8.2 / 1.0')
  await card.locator('tbody tr').first().click()
  await expect(page.locator('[data-quant-sentence="uav0001"]')).toContainText('斜距 182 m · 声学 30.9 dB(A) / 阈 33.0 · 余量 -2.1 dB · 概率 0.26')
  await page.locator('[data-quant-prob-note]').click()
  await expect(page.locator('[data-slot="popover-content"]')).toContainText('显示用概率，判定以确定性判据为准')
  await page.keyboard.press('Escape')
  // round 2 (d2-WP-13b-to-M15 item 6): the 360 px detail rail shows the compact columns without horizontal scrolling;
  // "all columns" brings the full table (scrolls inside the card)
  await expect(card).toHaveAttribute('data-quant-cols', 'compact')
  // every header cell of the compact table lies inside the card (no clipping by the table's own scroller either)
  const clipped = await card.evaluate((el) => {
    const r = el.getBoundingClientRect()
    return [...el.querySelectorAll('th, [role="columnheader"]')].filter((h) => h.getBoundingClientRect().right > r.right + 1).map((h) => h.textContent)
  })
  expect(clipped).toEqual([])
  await expect(card).toContainText('声学 / 阈')
  await expect(card).toContainText('判定')
  await shot(page, 'm15-d21-quant-card')
  await page.locator('[data-quant-cols-toggle]').click()
  await expect(card).toHaveAttribute('data-quant-cols', 'all')
  await expect(card).toContainText('等效像素')
  await shot(page, 'm15-d21-quant-card-all')
  await page.locator('[data-quant-cols-toggle]').click()
  await expect(card).toHaveAttribute('data-quant-cols', 'compact')
  // the session bar: the actions never cover the readouts (d2-INT-to-M15-M17 item 4, 1600 px)
  await expect(page.locator('[data-session-bar]')).toBeVisible()
  const overlapAt = () => page.evaluate(() => {
    const bar = document.querySelector('[data-session-bar]')
    const act = bar?.querySelector('[data-slot="alert-action"]')?.getBoundingClientRect()
    const desc = bar?.querySelector('[data-slot="alert-description"]')
    if (!act || !desc) return -1
    let right = 0
    for (const el of desc.querySelectorAll<HTMLElement>('[data-session-badge], [data-session-lifetime], [data-session-rate], [data-session-counts], [data-session-calendar-slot] > *')) {
      const r = el.getBoundingClientRect()
      if (r.width > 0) right = Math.max(right, r.right)
    }
    return right - act.left
  })
  expect(await overlapAt()).toBeLessThanOrEqual(0)
  await page.setViewportSize({ width: 1920, height: 1080 })
  await expect.poll(overlapAt).toBeLessThanOrEqual(0)
  await expect(page.locator('[data-session-counts]')).toBeVisible()
  await shot(page, 'm15-d21-session-bar-1920')
  await page.setViewportSize({ width: 1600, height: 900 })
  expect(s.posts.filter((p) => p.url.includes('quant'))).toEqual([])
  expect(w.errors).toEqual([])
})

test('sandbox power save: park after the threshold, resume through SB-70 on the first input', async ({ page, context }) => {
  test.skip(process.env.M15_UI_ONLY === '1', 'needs the real viewport (M06)')
  const w = watch(page)
  const s = { posts: [] as { url: string; body: unknown; headers: Record<string, string> }[], preview: 'ok' as const }
  await sandboxRoutes(context, s)
  const sessionJson = JSON.parse(readFileSync(join(import.meta.dirname, '..', '..', '..', '..', 'packages', 'contracts', 'fixtures', 'd2', 'rest', 'session.json'), 'utf8')) as Record<string, unknown>
  const future = () => String(BigInt(Date.now() + 1_800_000) * 1_000_000n)
  const calls: string[] = []
  await context.route(`**/api/sandbox/v1/sessions/${SID}/park`, (r) => {
    calls.push(`POST park ${r.request().postData() ?? ''}`)
    return r.fulfill({ status: 202, headers: { 'content-type': 'application/json' }, body: JSON.stringify({ state: 'PARKING' }) })
  })
  await context.route(`**/api/sandbox/v1/sessions/${SID}/resume`, (r) => {
    calls.push('POST resume')
    return r.fulfill({ status: 201, headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session: { ...sessionJson, state: 'READY', expires_unix_ns: future() }, token: TOKEN, token_exp_unix_ns: future() }) })
  })
  await storeSession(page)
  await page.goto(`/sandbox/${SID}?source=fake&fakeFixture=/__d2/sandbox_t5.awrrt&fakeLoop=1&reveal=shell&power=sandbox&powerIdle=12&powerWarn=4&powerQuiet=6`)
  await testBuild(page)
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  await page.mouse.move(500, 400)
  await page.mouse.move(700, 450, { steps: 5 })
  const overlay = page.locator('[data-power-overlay="SAVING"]')
  await expect(overlay).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-power-role="sandbox"]')).toContainText('沙盒已保存并暂停')
  await expect.poll(() => calls.some((c) => c.startsWith('POST park') && c.includes('idle'))).toBe(true)
  await shot(page, 'm15-d21-sandbox-parked')
  await page.keyboard.press('KeyX')
  await expect.poll(() => calls.includes('POST resume'), { timeout: 20_000 }).toBe(true)
  await expect.poll(() => page.evaluate(() => (window as unknown as Ux).__ux.power.state), { timeout: 30_000 }).toBe('ACTIVE')
  await expect(page.locator('[data-power-overlay]')).toHaveCount(0)
  expect(w.errors).toEqual([])
})

test('commander pairing (P1): the holder shows a code, /pair redeems it and opens the sandbox', async ({ page, context }) => {
  const w = watch(page)
  const s = { posts: [] as { url: string; body: unknown; headers: Record<string, string> }[], preview: 'ok' as const }
  await sandboxRoutes(context, s)
  const sessionJson = JSON.parse(readFileSync(join(import.meta.dirname, '..', '..', '..', '..', 'packages', 'contracts', 'fixtures', 'd2', 'rest', 'session.json'), 'utf8')) as Record<string, unknown>
  const future = (ms: number) => String(BigInt(Date.now() + ms) * 1_000_000n)
  await context.route(`**/api/sandbox/v1/sessions/${SID}/pair-code`, (r) => r.fulfill({ status: 201, headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code: 'K7M2Q9XA', expires_unix_ns: future(120_000) }) }))
  let redeemed: unknown = null
  await context.route(`**/api/sandbox/v1/sessions/${SID}/pair`, (r) => {
    redeemed = r.request().postDataJSON()
    return r.fulfill({ status: 200, headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session: { ...sessionJson, sid: SID, expires_unix_ns: future(1_800_000) }, token: TOKEN, token_exp_unix_ns: future(1_800_000) }) })
  })
  await storeSession(page)
  await openSandbox(page)
  await page.getByRole('menuitem', { name: '世界' }).click()
  await page.locator('[data-menu-pair]').click()
  await expect(page.locator('[data-pair-code="K7M2Q9XA"]')).toBeVisible()
  await shot(page, 'm15-d21-pair-holder')
  // the tablet (a fresh browser context without the session)
  const tablet = await page.context().browser()!.newContext({ baseURL: baseUrl(PORT), viewport: { width: 1194, height: 834 }, hasTouch: true, isMobile: true })
  await sandboxRoutes(tablet, s)
  await tablet.route(`**/api/sandbox/v1/sessions/${SID}/pair`, (r) => {
    redeemed = r.request().postDataJSON()
    return r.fulfill({ status: 200, headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session: { ...sessionJson, sid: SID, expires_unix_ns: future(1_800_000) }, token: TOKEN, token_exp_unix_ns: future(1_800_000) }) })
  })
  const p2 = await tablet.newPage()
  await p2.goto(`/pair?sid=${SID}&viewport=off&reveal=shell`)
  await expect(p2.locator("#boot-mask")).toHaveCount(0, { timeout: 60_000 })
  await expect(p2.locator('[data-view="pair"]')).toBeVisible({ timeout: 60_000 })
  await expect(p2.locator('[data-pair-sid]')).toHaveValue(SID)
  await p2.locator('[data-pair-input]').fill('k7m2q9xa')
  await shot(p2, 'm15-d21-pair-join')
  await p2.locator('[data-pair-join]').click()
  await expect(p2).toHaveURL(new RegExp(`/sandbox/${SID}`), { timeout: 20_000 })
  expect(redeemed).toEqual({ code: 'K7M2Q9XA' })
  expect(await p2.evaluate(() => JSON.parse(sessionStorage.getItem('awr.sandbox.session.v1') ?? '{}').sid)).toBe(SID)
  await tablet.close()
  expect(w.errors).toEqual([])
})
