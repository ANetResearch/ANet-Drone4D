// Phones and tablets on the real stack (WP-13b, M16; D2-AC-47 layout, D2-AC-48 quick commands; R-D2-35 to R-D2-37; AWR-04
// §15.8). Playwright device emulation (isMobile, hasTouch, device pixel ratio, mobile user agents; Tier S SwiftShader canvas) of
// iPhone 15 (portrait and landscape), Pixel 8 and iPad Pro 11 (portrait and landscape) against perf/d21/live21.ts
// (tests/e2e/sbstack.py: the shared show on S0 and the sandbox pool). Test build (D21_DIST).
//   1. viewer main flow on every device: the shared show reveals, the form factor and shell are right; phones switch the five
//      windows one at a time (the fleet window lists the live vehicles), tablets show the docked list and the task strip
//      (landscape) or the drawer (portrait); no horizontal scroll on any window, every control >= 44 x 44 CSS px (touch audit
//      0 violations), no forbidden glyph, no page error;
//   2. phone operator (iPhone 15, a real T6 session held by the page): quick commands hover and return in <= 4 taps with the
//      vehicle selected, each answered by the "指令已受理" Toast of the live sandbox; "watch" on the static person creates a
//      point_watch task;
//   3. tablet commander (iPad Pro 11 landscape, the same kind of session): the task strip's hover on the selected vehicle is
//      accepted; rotating to portrait keeps the selection and the link (session bar, LIVE), the drawer appears, no overflow;
//   4. tablet virtual stick (round 2, d2-FX-R2-web-to-M16 item 3; D2-AC-48 "平板虚拟摇杆 setpoint >= 25 Hz，旋转屏幕时归零"):
//      take over the flying p600-01, hold the left stick off-centre with CDP touch events: the manual pump's setpoints
//      (`__ux.sandbox.setpoints`) per second (>= 25 asserted with M16_PERF=1, >= 10 in functional runs, the measured rate
//      recorded); rotating while held zeroes the stick (`__ux.manual.stick` all 0, knob `translate(0px, 0px)`) and the
//      takeover stays ACTIVE (the idle keep-alive sends zero setpoints, so the setpoint count is no zeroing criterion).
// On a missed "指令已受理" Toast the page screenshot and the Toast log are attached (d2-WP-12c-to-M16 item 8).
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import {
  baseUrl, createSession, DEVICES, deviceContext, endSession, ensureBuild, glyphScan, holdSession, isTestBuild, noHorizontalScroll,
  PERF, pageHeld, recordToasts, revealed, shot, skipOnboarding, startLive21, startLive21Preview, touchAudit, waitPoolEmpty, waitState, watch,
  type Held, type Live21,
} from './live21'

const PORT = 4200
const DEMO_PORT = 4201
let live: Live21 | null = null
let server: PreviewServer | null = null
let demo: PreviewServer | null = null

test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => {
  test.setTimeout(600_000)
  const dist = ensureBuild('test')
  live = await startLive21({ maxSessions: 2, releaseFrom: dist, set: ['run.scenario=s0-synthcity-showcase'] })
  server = await startLive21Preview(PORT, live, dist)
  demo = await startLive21Preview(DEMO_PORT, live, ensureBuild('demo'))
})
test.afterAll(async () => {
  await server?.close()
  await demo?.close()
  await live?.stop()
})

type Ux = { __ux: { quick: { state: string; sent: string[] }; rtCalls: string[]; selection: { ids: string[] }; power: { conn?: string } } }
const ux = (page: Page): Promise<Ux['__ux']> => page.evaluate(() => (window as unknown as Ux).__ux)

async function ready(page: Page): Promise<void> {
  await revealed(page)
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 90_000 })
}

for (const d of DEVICES) {
  test(`viewer main flow: ${d.name} (${d.w} x ${d.h})`, async ({ browser }) => {
    test.setTimeout(300_000)
    const ctx = await deviceContext(browser, d, baseUrl(PORT))
    const page = await ctx.newPage()
    const w = watch(page)
    await skipOnboarding(page)
    await page.goto('/world/synthcity')
    await ready(page)
    await expect(page.locator('html')).toHaveAttribute('data-ff', d.ff)
    await expect(page.locator('html')).toHaveAttribute('data-coarse', '')
    await expect(page.locator(`[data-shell="${d.ff}"]`)).toBeAttached()
    await page.waitForTimeout(1500)
    await noHorizontalScroll(page)
    expect(await touchAudit(page)).toEqual([])
    if (d.ff === 'phone') {
      await expect(page.locator(d.orient === 'portrait' ? '[data-tab-bar="bottom"]' : '[data-tab-bar="vertical"]')).toBeVisible()
      for (const tab of ['fleet', 'tasks', 'targets', 'alerts', 'viewport'] as const) {
        await page.locator(`[data-tab="${tab}"]`).tap()
        await expect(page.locator('[data-shell="phone"]')).toHaveAttribute('data-tab', tab)
        if (tab === 'viewport') await expect(page.locator('[data-window][data-active]')).toHaveCount(0)   // the canvas itself
        else {
          await expect(page.locator(`[data-window="${tab}"][data-active]`)).toBeVisible()
          await expect(page.locator('[data-window][data-active]')).toHaveCount(1)
        }
        if (tab === 'fleet') await expect(page.locator('[data-window="fleet"] [data-drone-id]').first()).toBeVisible({ timeout: 30_000 })
        await noHorizontalScroll(page)
        expect(await touchAudit(page), tab).toEqual([])
        await shot(page, `d21-mobile-live-${d.name}-${tab}`)
      }
    } else if (d.orient === 'landscape') {
      await expect(page.locator('[data-dock="left"] [data-drone-id]').first()).toBeVisible({ timeout: 30_000 })
      await expect(page.locator('[data-task-strip]')).toBeVisible()
      await shot(page, `d21-mobile-live-${d.name}`)
    } else {
      await expect(page.locator('[data-bottom-drawer]')).toBeVisible()
      await shot(page, `d21-mobile-live-${d.name}`)
    }
    expect(await glyphScan(page)).toEqual([])
    expect(w.errors).toEqual([])
    await ctx.close()
  })
}

test('public demo build on a phone: the shared show viewer has the five windows and no command entry', async ({ browser }) => {
  test.setTimeout(300_000)
  const ctx = await deviceContext(browser, DEVICES[2], baseUrl(DEMO_PORT))
  const page = await ctx.newPage()
  const w = watch(page)
  await skipOnboarding(page)
  await page.goto('/world/synthcity')
  await revealed(page)
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 90_000 })
  await expect(page.locator('[data-shell="phone"]')).toBeAttached()
  await expect(page.locator('[data-tab-bar="bottom"]')).toBeVisible()
  await expect(page.locator('[data-phone-command]')).toHaveCount(0)                       // a viewer gives no commands
  for (const tab of ['fleet', 'targets', 'alerts', 'viewport'] as const) {
    await page.locator(`[data-tab="${tab}"]`).tap()
    await expect(page.locator('[data-shell="phone"]')).toHaveAttribute('data-tab', tab)
    await noHorizontalScroll(page)
    expect(await touchAudit(page), tab).toEqual([])
  }
  await shot(page, 'd21-mobile-live-demo-pixel8')
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors).toEqual([])
  await ctx.close()
})

async function quickTaps(page: Page, op: 'hover' | 'rtl' | 'watch'): Promise<number> {
  let taps = 0
  const tap = async (sel: string): Promise<void> => { await page.locator(sel).first().tap(); taps++ }
  await tap('[data-phone-command]')
  const sheet = page.locator('[data-quick-sheet]')
  await expect(sheet).toBeVisible()
  if ((await sheet.getAttribute('data-quick-sheet')) === 'SELECT') {
    await tap('[data-quick-vehicle]')
    await tap('[data-quick-next]')
  }
  await expect(page.locator('[data-quick-sheet="ACTION"]')).toBeVisible()
  await tap(`[data-quick-op="${op}"]`)
  if (op === 'watch') {
    await expect(page.locator('[data-quick-sheet="PICK_TARGET"]')).toBeVisible()
    await tap('[data-quick-target="person-01"]')
  }
  await expect(page.locator(`[data-quick-confirm="${op}"]`)).toBeVisible()
  await tap('[data-quick-send]')
  await expect(page.locator('[data-quick-sheet]')).toHaveCount(0)
  return taps
}

const toastLogs = new WeakMap<Page, () => Promise<string[]>>()
/** n "指令已受理" Toasts so far (from the page's toast log) and no "指令未受理" */
async function accepted(page: Page, n: number): Promise<void> {
  const log = toastLogs.get(page)!
  try {
    await expect.poll(async () => (await log()).filter((x) => x.includes('指令已受理')).length, { timeout: 20_000 }).toBeGreaterThanOrEqual(n)
  } catch (e) {
    // tell a late first result of the server from a missing Toast (d2-WP-12c-to-M16 item 8)
    const info = test.info()
    await info.attach('toast-log.json', { body: JSON.stringify({ toasts: await log().catch(() => []),
      calls: await page.evaluate(() => (window as unknown as Ux).__ux.rtCalls.slice(-20)).catch(() => []) }, null, 1), contentType: 'application/json' })
    await info.attach('page.png', { body: await page.screenshot().catch(() => Buffer.alloc(0)), contentType: 'image/png' })
    throw e
  }
  expect((await log()).filter((x) => x.includes('指令未受理'))).toEqual([])
}

async function tasks(h: Held): Promise<{ type?: string; kind?: string; params?: { target?: string } }[]> {
  const r = await fetch(`${live!.base}/api/sandbox/v1/sessions/${h.sid}/tasks`, { headers: { authorization: `Bearer ${h.token}` } })
  return ((await r.json()) as { items?: never[] }).items ?? []
}

/** p600-01 is airborne (a return or hover on the ground is refused with 105 STATE; under load the take-off of the T6 session
 *  can take longer than the page's start, d2-WP-12c-to-M16 item 8, diagnosed in round 2 from the attached Toast log) */
async function waitFlying(h: Held): Promise<void> {
  await expect.poll(async () => {
    const r = await fetch(`${live!.base}/api/sandbox/v1/sessions/${h.sid}/vehicles/p600-01/state`, { headers: { authorization: `Bearer ${h.token}` } })
    const j = await r.json() as { state?: { flight_state?: string } }
    return j.state?.flight_state ?? ''
  }, { timeout: 180_000, intervals: [2000] }).toBe('FLYING')
}

async function phoneSession(browser: import('@playwright/test').Browser, hint: string): Promise<{ page: Page; h: Held; ctx: import('@playwright/test').BrowserContext; w: ReturnType<typeof watch> }> {
  const ctx = await deviceContext(browser, DEVICES[0], baseUrl(PORT))
  const page = await ctx.newPage()
  const w = watch(page)
  toastLogs.set(page, await recordToasts(page))
  const h = await createSession(live!, hint, 't6_manual')
  await waitState(live!, h, ['READY', 'ACTIVE'])
  await holdSession(page, h)
  await page.goto(`/sandbox/${h.sid}`)
  await ready(page)
  await expect(page.locator('[data-shell="phone"]')).toBeAttached()
  await expect(page.locator('[data-phone-command]')).toBeVisible({ timeout: 60_000 })
  // select the vehicle in the fleet window, back to the viewport
  await page.locator('[data-tab="fleet"]').tap()
  await page.locator('[data-window="fleet"] [data-drone-id="p600-01"]').first().tap()
  await page.locator('[data-tab="viewport"]').tap()
  await expect.poll(async () => (await ux(page)).selection.ids, { timeout: 10_000 }).toContain('p600-01')
  return { page, h, ctx, w }
}

async function closePhone(x: { page: Page; h: Held; ctx: import('@playwright/test').BrowserContext } | null): Promise<void> {
  if (!x) return
  const held = await pageHeld(x.page).catch(() => null)
  await endSession(live!, held ?? x.h)
  await x.ctx.close()
  await waitPoolEmpty(live!).catch(() => null)
}

test('phone operator: hover and return in <= 4 taps each, accepted by the live sandbox', async ({ browser }) => {
  test.setTimeout(300_000)
  let x: Awaited<ReturnType<typeof phoneSession>> | null = null
  try {
    x = await phoneSession(browser, 'MSIXMOBILEPHONEHINTAAAAA')
    const { page, w } = x
    await waitFlying((await pageHeld(page)) ?? x.h)
    const t1 = await quickTaps(page, 'hover')
    expect(t1).toBeLessThanOrEqual(4)
    await accepted(page, 1)
    const t3 = await quickTaps(page, 'rtl')
    expect(t3).toBeLessThanOrEqual(4)
    // the server took the return command: the vehicle state turns to RTL (its "accepted" Toast is checked by the next-but-one
    // case)
    const h = (await pageHeld(page)) ?? x.h
    await expect.poll(async () => {
      const r = await fetch(`${live!.base}/api/sandbox/v1/sessions/${h.sid}/vehicles/p600-01/state`, { headers: { authorization: `Bearer ${h.token}` } })
      return JSON.stringify(await r.json()).toUpperCase().includes('RTL')
    }, { timeout: 20_000, intervals: [500] }).toBe(true)
    const u = await ux(page)
    expect(u.quick.sent.slice(-2).map((s) => s.split(':')[0])).toEqual(['hover', 'rtl'])
    expect(u.rtCalls.some((c) => /cmd\/hover$/.test(c))).toBe(true)
    expect(u.rtCalls.some((c) => /cmd\/rtl$/.test(c))).toBe(true)
    await shot(page, 'd21-mobile-live-phone-commands')
    await noHorizontalScroll(page)
    expect(w.errors).toEqual([])
  } finally {
    await closePhone(x)
  }
})

test('phone operator: "watch" lists the session targets and creates a point_watch task', async ({ browser }) => {
  // d2-WP-13b-to-M15 item 1 (fixed in D2-INT): the quick command's target step reads the targets store, which the realtime
  // TargetLite32 rows fill since net/rt/decode.ts hands raw rows to the data path as bytes.
  test.setTimeout(240_000)
  let x: Awaited<ReturnType<typeof phoneSession>> | null = null
  try {
    x = await phoneSession(browser, 'MSIXMOBILEWATCHHINTAAAAA')
    const { page } = x
    const tasks0 = (await tasks((await pageHeld(page)) ?? x.h)).length
    await page.locator('[data-phone-command]').tap()
    await expect(page.locator('[data-quick-sheet="ACTION"]')).toBeVisible()
    await page.locator('[data-quick-op="watch"]').tap()
    await expect(page.locator('[data-quick-sheet="PICK_TARGET"]')).toBeVisible()
    await expect(page.locator('[data-quick-target="person-01"]')).toBeVisible({ timeout: 20_000 })
    await page.locator('[data-quick-target="person-01"]').tap()
    await page.locator('[data-quick-send]').tap()
    await accepted(page, 1)
    await expect.poll(async () => (await tasks((await pageHeld(page)) ?? x!.h)).length, { timeout: 20_000 }).toBeGreaterThan(tasks0)
    const tl = await tasks((await pageHeld(page)) ?? x.h)
    expect(tl.some((t) => (t.type ?? t.kind) === 'point_watch')).toBe(true)
  } finally {
    await closePhone(x)
  }
})

test('phone operator: the return command is acknowledged within 20 s', async ({ browser }) => {
  // d2-WP-13b-to-M15 item 2 (fixed in D2-INT): the quick command's Toast follows the first `accepted` result
  // (CallHandle.onResult), so a return is acknowledged long before the landing (D2-AC-48 "收到受理结果").
  test.setTimeout(240_000)
  let x: Awaited<ReturnType<typeof phoneSession>> | null = null
  try {
    x = await phoneSession(browser, 'MSIXMOBILERTLACKHINTAAAA')
    await waitFlying((await pageHeld(x.page)) ?? x.h)
    await quickTaps(x.page, 'rtl')
    await accepted(x.page, 1)
  } finally {
    await closePhone(x)
  }
})

test('tablet commander: task strip hover accepted; rotation keeps the selection and the link', async ({ browser }) => {
  test.setTimeout(420_000)
  const d = DEVICES[4]
  let h: Held | null = null
  const ctx = await deviceContext(browser, d, baseUrl(PORT))
  const page = await ctx.newPage()
  const w = watch(page)
  toastLogs.set(page, await recordToasts(page))
  try {
    h = await createSession(live!, 'MSIXMOBILETABLETHINTAAAA', 't6_manual')
    await waitState(live!, h, ['READY', 'ACTIVE'])
    await holdSession(page, h)
    await page.goto(`/sandbox/${h.sid}`)
    await ready(page)
    await expect(page.locator('[data-shell="tablet"][data-orient="landscape"]')).toBeAttached()
    await waitFlying(h)
    await page.locator('[data-dock="left"] [data-drone-id="p600-01"]').first().tap()
    await expect(page.locator('[data-strip-op="hover"]')).toBeEnabled({ timeout: 15_000 })
    await page.locator('[data-strip-op="hover"]').tap()
    await expect(page.locator('[data-quick-confirm="hover"]')).toBeVisible()
    await page.locator('[data-quick-send]').tap()
    await accepted(page, 1)
    await noHorizontalScroll(page)
    expect(await touchAudit(page)).toEqual([])
    await shot(page, 'd21-mobile-live-tablet-land')
    const sel = (await ux(page)).selection.ids
    await page.setViewportSize({ width: 834, height: 1194 })
    await expect(page.locator('[data-shell="tablet"][data-orient="portrait"]')).toBeAttached({ timeout: 5_000 })
    await expect(page.locator('[data-bottom-drawer]')).toBeVisible()
    expect((await ux(page)).selection.ids).toEqual(sel)
    await expect.poll(async () => String((await ux(page)).power.conn ?? 'LIVE'), { timeout: 10_000 }).toMatch(/^(LIVE|DEGRADED)/)
    await noHorizontalScroll(page)
    expect(await touchAudit(page)).toEqual([])
    await shot(page, 'd21-mobile-live-tablet-portrait')
    expect(await glyphScan(page)).toEqual([])
    expect(w.errors).toEqual([])
  } finally {
    const held = await pageHeld(page).catch(() => null)
    await endSession(live!, held ?? h)
    await ctx.close()
    await waitPoolEmpty(live!).catch(() => null)
  }
})

type Manual = { state: string; stick: number[]; ended: string }
type UxStick = { __ux: { sandbox: { setpoints: number }; manual: Manual } }
const stickUx = (page: Page): Promise<UxStick['__ux']> => page.evaluate(() => (window as unknown as UxStick).__ux)

test('tablet commander: the virtual stick sends setpoints at the pump rate and a rotation zeroes it', async ({ browser }) => {
  test.setTimeout(480_000)
  const d = DEVICES[4]
  let h: Held | null = null
  const ctx = await deviceContext(browser, d, baseUrl(PORT))
  const page = await ctx.newPage()
  const w = watch(page)
  try {
    h = await createSession(live!, 'MSIXMOBILESTICKHINTAAAAA', 't6_manual')
    await waitState(live!, h, ['READY', 'ACTIVE'])
    await holdSession(page, h)
    await page.goto(`/sandbox/${h.sid}?reveal=shell&viewport=off`)
    await revealed(page)
    test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
    await expect(page.locator('[data-shell="tablet"][data-orient="landscape"]')).toBeAttached({ timeout: 30_000 })
    await waitFlying(h)
    await page.locator('[data-dock="left"] [data-drone-id="p600-01"]').first().tap()
    const take = page.locator('[data-manual-takeover="p600-01"]').first()
    await expect(take).toBeVisible({ timeout: 15_000 })
    await take.tap()
    await page.locator('[data-manual-confirm-go]').tap()
    await expect(page.locator('[data-manual-bar]')).toBeVisible({ timeout: 30_000 })
    await expect.poll(async () => (await stickUx(page)).manual.state, { timeout: 15_000 }).toBe('ACTIVE')
    const base = page.locator('[data-stick="left"] > div').first()
    await expect(base).toBeVisible({ timeout: 15_000 })
    const b = (await base.boundingBox())!
    const cx = b.x + b.width / 2
    const cy = b.y + b.height / 2
    const cdp = await ctx.newCDPSession(page)
    const touch = (type: string, x?: number, y?: number): Promise<unknown> => cdp.send('Input.dispatchTouchEvent',
      { type, touchPoints: x === undefined ? [] : [{ x, y: y!, id: 1, radiusX: 4, radiusY: 4, force: 1 }] } as never)
    let ended = false
    try {
      await touch('touchStart', cx, cy)
      for (let i = 1; i <= 4; i++) await touch('touchMove', cx, cy - b.height * 0.1 * i)
      await expect.poll(async () => (await stickUx(page)).manual.stick.some((v) => v !== 0), { timeout: 5_000 }).toBe(true)
      const n0 = (await stickUx(page)).sandbox.setpoints
      const t0 = Date.now()
      await page.waitForTimeout(2000)
      const hz = ((await stickUx(page)).sandbox.setpoints - n0) / ((Date.now() - t0) / 1000)
      test.info().annotations.push({ type: 'stick_setpoint_hz', description: hz.toFixed(1) })
      expect(hz).toBeGreaterThanOrEqual(PERF ? 25 : 10)
      // rotate while the stick is held: the stick is recentred and its axes are zero, the takeover goes on
      await page.setViewportSize({ width: 834, height: 1194 })
      await expect(page.locator('[data-shell="tablet"][data-orient="portrait"]')).toBeAttached({ timeout: 5_000 })
      await page.waitForTimeout(800)
      const m = (await stickUx(page)).manual
      test.info().annotations.push({ type: 'stick_after_rotation', description: JSON.stringify(m) })
      expect(m.stick.every((v) => v === 0)).toBe(true)
      expect(m.state).toBe('ACTIVE')
      const knob = await page.locator('[data-stick="left"] .stick-knob').first().evaluate((el) => (el as HTMLElement).style.transform).catch(() => null)
      if (knob !== null) expect(knob).toBe('translate(0px, 0px)')
      await touch('touchEnd')
      ended = true
    } finally {
      if (!ended) await touch('touchEnd').catch(() => null)
    }
    await noHorizontalScroll(page)
    await shot(page, 'd21-mobile-live-tablet-stick')
    expect(w.errors).toEqual([])
  } finally {
    const heldNow = await pageHeld(page).catch(() => null)
    await endSession(live!, heldNow ?? h)
    await ctx.close()
    await waitPoolEmpty(live!).catch(() => null)
  }
})
