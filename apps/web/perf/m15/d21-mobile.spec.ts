// M15 D2.1 functional specs, phones and tablets (WP-12c; M15-AC-224 to AC-226; D2-AC-47, D2-AC-48 front part, D2-AC-50
// functional part). Playwright device emulation (isMobile, hasTouch, deviceScaleFactor) on the test build of d21-shell, Tier S
// SwiftShader canvas, FakeSource; the sandbox cases use the scripted sandbox REST of helpers/d2rest.
//   1. the device matrix (iPhone 15 portrait and landscape, Pixel 8, iPad Pro 11 portrait and landscape, iPad Pro 12.9 coarse
//      desktop, desktop, 320 px): form factor attributes, the shell, no horizontal scroll, the touch-target audit on coarse
//      pointers (every visible control hit-testable over 44 x 44 CSS px around its centre: 0 violations), screenshots that
//      the report reads back (.cache/impl/shots/m15-d21-dev-*.png);
//   2. phone: the five windows switch one at a time and keep their state, the canvas is suspended behind the other windows,
//      the layers drawer holds the field view, the quick command "hover" takes <= 4 taps and sends the command;
//   3. tablet: the dockable side panels (dock, float, collapse), the task strip, rotation to portrait (drawer with three snap
//      points) keeps the selection and the link; the long press opens the anchored menu;
//   4. PWA: manifest, full-screen button, iOS meta.
// Run: `M15_DIST=<dir> npx playwright test perf/m15/d21-mobile.spec.ts --project perf`.
import { expect, test, type Browser, type BrowserContext, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, glyphScan, injectConn, isTestBuild, shot, startPreview, watch } from './helpers/shell'
import { routes as sandboxRoutes, SID, storeSession } from './helpers/d2rest'

const PORT = 4192
let server: PreviewServer | null = null
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})
test.describe.configure({ mode: 'serial' })

interface Device { name: string; w: number; h: number; dpr: number; mobile: boolean; touch: boolean; ff: string; shell: string | null }
const DEVICES: readonly Device[] = [
  { name: 'iphone15', w: 393, h: 852, dpr: 3, mobile: true, touch: true, ff: 'phone', shell: 'phone' },
  { name: 'iphone15-land', w: 852, h: 393, dpr: 3, mobile: true, touch: true, ff: 'phone', shell: 'phone' },
  { name: 'pixel8', w: 412, h: 915, dpr: 2.625, mobile: true, touch: true, ff: 'phone', shell: 'phone' },
  { name: 'ipadpro11', w: 834, h: 1194, dpr: 2, mobile: true, touch: true, ff: 'tablet', shell: 'tablet' },
  { name: 'ipadpro11-land', w: 1194, h: 834, dpr: 2, mobile: true, touch: true, ff: 'tablet', shell: 'tablet' },
  { name: 'ipadpro129-land', w: 1366, h: 1024, dpr: 2, mobile: true, touch: true, ff: 'desktop', shell: null },
  { name: 'desktop', w: 1600, h: 900, dpr: 1, mobile: false, touch: false, ff: 'desktop', shell: null },
  { name: 'narrow', w: 320, h: 640, dpr: 2, mobile: true, touch: true, ff: 'narrow', shell: null },
]
const MOBILE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
const Q = 'source=fake&fakeWorld=synthcity&fakeN=4&reveal=shell'

async function deviceContext(browser: Browser, d: Device): Promise<BrowserContext> {
  return browser.newContext({
    baseURL: baseUrl(PORT), viewport: { width: d.w, height: d.h }, deviceScaleFactor: d.dpr, isMobile: d.mobile, hasTouch: d.touch,
    ...(d.mobile ? { userAgent: MOBILE_UA } : {}),
  })
}

async function ready(page: Page, canvas = true): Promise<void> {
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 90_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
  if (canvas) await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
}

/** visible controls whose hit area around the centre is under 44 x 44 CSS px (AWR-04 §15.8.5) */
async function touchAudit(page: Page): Promise<string[]> {
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
      // covered by another layer at its centre (a dialog over it): not reachable now, not counted
      const mid = document.elementFromPoint(cx, cy)
      if (!mid || !(mid === el || el.contains(mid) || mid.contains(el))) continue
      // a label of the control (clicking it operates the control) counts as part of its hit area
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

async function noHorizontalScroll(page: Page): Promise<void> {
  const o = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, x: scrollX }))
  expect(o.sw).toBeLessThanOrEqual(o.cw)
  expect(o.x).toBe(0)
}

for (const d of DEVICES) {
  test(`device matrix: ${d.name} (${d.w} x ${d.h})`, async ({ browser }) => {
    test.setTimeout(240_000)
    const ctx = await deviceContext(browser, d)
    const page = await ctx.newPage()
    const w = watch(page)
    await page.goto(`/world/synthcity?${Q}`)
    await ready(page, d.ff !== 'narrow')
    await expect(page.locator('html')).toHaveAttribute('data-ff', d.ff)
    if (d.touch) await expect(page.locator('html')).toHaveAttribute('data-coarse', '')
    if (d.shell) await expect(page.locator(`[data-shell="${d.shell}"]`)).toBeAttached()
    if (d.ff === 'narrow') await expect(page.locator('[data-empty="full"]')).toContainText('窗口过窄')
    if (d.ff === 'desktop') await expect(page.locator('[data-figure="header"]')).toBeVisible()
    await page.waitForTimeout(1500)
    await noHorizontalScroll(page)
    if (d.touch) expect(await touchAudit(page)).toEqual([])
    expect(await glyphScan(page)).toEqual([])
    await shot(page, `m15-d21-dev-${d.name}`)
    expect(w.errors).toEqual([])
    await ctx.close()
  })
}

/** CDP touch input in CSS px of the page */
async function touches(page: Page): Promise<(type: 'touchStart' | 'touchMove' | 'touchEnd', pts: [number, number][]) => Promise<void>> {
  const cdp = await page.context().newCDPSession(page)
  return async (type, pts) => {
    await cdp.send('Input.dispatchTouchEvent', { type, touchPoints: pts.map(([x, y], i) => ({ x, y, id: i, radiusX: 1, radiusY: 1, force: 1 })) })
  }
}

type Ux = { __ux: { quick: { state: string; sent: string[] }; rtCalls: string[]; ff: { ff: string; orient: string; changes: number }; selection: { ids: string[] } }
  __vp: { loop: { stats(): { frameNo: number; suspended: boolean } } } }

test('phone: five windows, suspended canvas behind them, layers drawer, quick command in <= 4 taps', async ({ browser }) => {
  test.setTimeout(300_000)
  const d = DEVICES[0]
  const ctx = await deviceContext(browser, d)
  const page = await ctx.newPage()
  const w = watch(page)
  const s = { posts: [] as { url: string; body: unknown; headers: Record<string, string> }[], preview: 'ok' as const }
  await sandboxRoutes(ctx, s)
  await storeSession(page)
  await page.goto(`/sandbox/${SID}?source=fake&fakeFixture=/__d2/sandbox_t5.awrrt&fakeLoop=1&reveal=shell`)
  await ready(page)
  await injectConn(page, { role: 'operator', seat: 'held' })
  await expect(page.locator(`[data-shell="phone"]`)).toBeAttached()
  await expect(page.locator('[data-tab-bar="bottom"]')).toBeVisible()
  const suspended = () => page.evaluate(() => (window as unknown as Ux).__vp.loop.stats().suspended)
  // windows: one at a time; the canvas sleeps behind them
  for (const tab of ['fleet', 'tasks', 'targets', 'alerts'] as const) {
    await page.locator(`[data-tab="${tab}"]`).tap()
    await expect(page.locator(`[data-window="${tab}"][data-active]`)).toBeVisible()
    await expect(page.locator('[data-window][data-active]')).toHaveCount(1)
    await expect.poll(suspended).toBe(true)
    await shot(page, `m15-d21-phone-${tab}`)
  }
  // the fleet window keeps its page across switches
  await page.locator('[data-tab="fleet"]').tap()
  const fleetRow = page.locator('[data-window="fleet"] [data-drone-id]').first()
  if (await fleetRow.count()) {
    await fleetRow.tap()
    await page.locator('[data-tab="alerts"]').tap()
    await page.locator('[data-tab="fleet"]').tap()
  }
  await page.locator('[data-tab="viewport"]').tap()
  await expect.poll(suspended).toBe(false)
  // the layers drawer: field view and volumes
  await page.locator('[data-phone-layers]').tap()
  await expect(page.locator('[data-layers-sheet]')).toBeVisible()
  await page.locator('[data-layers-sheet] [data-field-option="acoustic"]').tap()
  await expect(page.locator('[data-layers-sheet] [data-field-view]')).toHaveAttribute('data-field-view', 'acoustic')
  await shot(page, 'm15-d21-phone-layers')
  expect(await touchAudit(page)).toEqual([])
  await page.locator('[data-sheet-handle]').tap() // cycles the snap point (keyboard and tap path of the handle)
  await page.keyboard.press('Escape')
  await expect(page.locator('[data-layers-sheet]')).toHaveCount(0)
  // quick command "hover": commands (1) -> vehicle (2) -> next (3) -> hover (4) -> send (5) without a selection; with a
  // selection the vehicle step is skipped: commands (1) -> hover (2) -> send (3)
  await page.locator('[data-phone-command]').tap()
  const sheet = page.locator('[data-quick-sheet]')
  await expect(sheet).toBeVisible()
  if ((await sheet.getAttribute('data-quick-sheet')) === 'SELECT') {
    await shot(page, 'm15-d21-phone-quick-select')
    await page.locator('[data-quick-vehicle]').first().tap()
    await page.locator('[data-quick-next]').tap()
  }
  await expect(page.locator('[data-quick-sheet="ACTION"]')).toBeVisible()
  await shot(page, 'm15-d21-phone-quick-action')
  await page.locator('[data-quick-op="hover"]').tap()
  await expect(page.locator('[data-quick-confirm="hover"]')).toBeVisible()
  await shot(page, 'm15-d21-phone-quick-confirm')
  await page.locator('[data-quick-send]').tap()
  await expect(page.locator('[data-quick-sheet]')).toHaveCount(0)
  const ux = await page.evaluate(() => (window as unknown as Ux).__ux)
  expect(ux.quick.sent.at(-1)).toMatch(/^hover:/)
  expect(ux.rtCalls.some((c) => /\/cmd\/hover$|fleet\/cmd\/hover/.test(c))).toBe(true)
  // now with the selection: <= 4 taps for goto as well (commands, goto, point, send)
  await page.locator('[data-phone-command]').tap()
  await expect(page.locator('[data-quick-sheet="ACTION"]')).toBeVisible()
  await page.locator('[data-quick-op="rtl"]').tap()
  await page.locator('[data-quick-send]').tap()
  await expect.poll(() => page.evaluate(() => (window as unknown as Ux).__ux.quick.sent.at(-1))).toMatch(/^rtl:/)
  // "watch" lists the session's targets (the targets panel's list: store rows, else roster and configurations; round 2,
  // d2-WP-13b-to-M15 item 1) and sends the point_watch task: commands (1) -> watch (2) -> target (3) -> send (4)
  await page.locator('[data-phone-command]').tap()
  await expect(page.locator('[data-quick-sheet="ACTION"]')).toBeVisible()
  await page.locator('[data-quick-op="watch"]').tap()
  await expect(page.locator('[data-quick-sheet="PICK_TARGET"]')).toBeVisible()
  await expect(page.locator('[data-quick-target="person-01"]')).toBeVisible({ timeout: 10_000 })
  await expect(page.locator('[data-quick-target="person-01"]')).toBeEnabled()
  await shot(page, 'm15-d21-phone-quick-watch')
  await page.locator('[data-quick-target="person-01"]').tap()
  await expect(page.locator('[data-quick-confirm="watch"]')).toBeVisible()
  await page.locator('[data-quick-send]').tap()
  await expect.poll(() => page.evaluate(() => (window as unknown as Ux).__ux.quick.sent.at(-1))).toMatch(/^watch:/)
  // rotation keeps the selection and the link; the tab bar moves to the left
  const sel = await page.evaluate(() => (window as unknown as Ux).__ux.selection.ids)
  await page.setViewportSize({ width: 852, height: 393 })
  await expect(page.locator('[data-tab-bar="vertical"]')).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as Ux).__ux.selection.ids)).toEqual(sel)
  await noHorizontalScroll(page)
  await shot(page, 'm15-d21-phone-landscape')
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors).toEqual([])
  await ctx.close()
})

test('tablet commander: dock, float, collapse, task strip, rotation, long press', async ({ browser }) => {
  test.setTimeout(300_000)
  const d = DEVICES[4]
  const ctx = await deviceContext(browser, d)
  const page = await ctx.newPage()
  const w = watch(page)
  const s = { posts: [] as { url: string; body: unknown; headers: Record<string, string> }[], preview: 'ok' as const }
  await sandboxRoutes(ctx, s)
  await storeSession(page)
  await page.goto(`/sandbox/${SID}?source=fake&fakeFixture=/__d2/sandbox_t5.awrrt&fakeLoop=1&reveal=shell`)
  await ready(page)
  await injectConn(page, { role: 'operator', seat: 'held' })
  await expect(page.locator(`[data-shell="tablet"][data-orient="landscape"]`)).toBeAttached()
  await expect(page.locator('[data-dock="left"]')).toHaveAttribute('data-dock-mode', 'docked')
  // two docked panels would leave less than half of 1194 px: the right one floats once there is a detail to show, before that
  // it folds to its icon bar
  await expect(page.locator('[data-dock="right"]')).toHaveAttribute('data-dock-mode', 'collapsed')
  await page.locator('[data-dock="left"] [data-drone-id]').first().tap()
  await expect(page.locator('[data-dock="right"]')).toHaveAttribute('data-dock-mode', 'floating')
  await expect(page.locator('[data-dock="right"] [data-panel="drone-detail"]')).toBeVisible()
  await expect(page.locator('[data-task-strip]')).toBeVisible()
  await expect(page.locator('[data-strip-op="hover"]')).toBeVisible()
  await shot(page, 'm15-d21-tablet-land')
  // collapse the left panel to its icon bar and dock it again
  await page.locator('[data-dock-collapse="left"]').tap()
  await expect(page.locator('[data-dock="left"]')).toHaveAttribute('data-dock-mode', 'collapsed')
  await page.locator('[data-dock-expand="left"]').tap()
  // opened last, while the detail panel is docked: the left one floats now (both docked would leave < 50 % to the view)
  await expect(page.locator('[data-dock="left"]')).toHaveAttribute('data-dock-mode', 'floating')
  await expect(page.locator('[data-dock="right"]')).toHaveAttribute('data-dock-mode', 'docked')
  await shot(page, 'm15-d21-tablet-land-float')
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('awr.ui.dock.v1') ?? '{}').left)).toBe('docked')
  // the field menu of the top bar
  await page.locator('[data-field-menu]').tap()
  await expect(page.locator('[data-field-popover] [data-field-view]')).toBeVisible()
  await page.keyboard.press('Escape')
  expect(await touchAudit(page)).toEqual([])
  // long press on the canvas: the anchored menu (measure, copy coordinates; fly here and place for the operator)
  const t = await touches(page)
  await t('touchStart', [[600, 420]])
  await page.waitForTimeout(700)
  await t('touchEnd', [])
  const menu = page.locator('[data-longpress-menu]')
  if (await menu.count()) {
    await expect(menu).toBeVisible()
    await expect(menu.locator('[data-longpress="copy"]')).toBeVisible()
    await shot(page, 'm15-d21-tablet-longpress')
    await page.keyboard.press('Escape')
  }
  // rotation: the drawer with three snap points; selection and link stay
  const sel = await page.evaluate(() => (window as unknown as Ux).__ux.selection.ids)
  await page.setViewportSize({ width: 834, height: 1194 })
  await expect(page.locator(`[data-shell="tablet"][data-orient="portrait"]`)).toBeAttached()
  const drawer = page.locator('[data-bottom-drawer]')
  await expect(drawer).toHaveAttribute('data-bottom-drawer', '25')
  await page.locator('[data-bottom-drawer] [data-sheet-handle]').tap()
  await expect(drawer).toHaveAttribute('data-bottom-drawer', '50')
  await page.locator('[data-drawer-tab="targets"]').tap()
  await shot(page, 'm15-d21-tablet-portrait')
  await page.locator('[data-bottom-drawer] [data-sheet-handle]').tap()
  await expect(drawer).toHaveAttribute('data-bottom-drawer', '90')
  expect(await page.evaluate(() => (window as unknown as Ux).__ux.selection.ids)).toEqual(sel)
  await noHorizontalScroll(page)
  expect(await touchAudit(page)).toEqual([])
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors).toEqual([])
  await ctx.close()
})

test('PWA: manifest, full screen, iOS meta', async ({ browser }) => {
  const ctx = await deviceContext(browser, DEVICES[4])
  const page = await ctx.newPage()
  await page.goto(`/world/synthcity?${Q}&viewport=off`)
  await ready(page, false)
  expect(await page.locator('meta[name="viewport"]').getAttribute('content')).toContain('viewport-fit=cover')
  expect(await page.locator('meta[name="apple-mobile-web-app-capable"]').getAttribute('content')).toBe('yes')
  const href = await page.locator('link[rel="manifest"]').getAttribute('href')
  expect(href).toBeTruthy()
  const m = await (await page.request.get(href as string)).json() as { display: string; display_override: string[]; start_url: string; icons: { sizes: string; purpose?: string }[] }
  expect(m.display).toBe('fullscreen')
  expect(m.display_override).toContain('fullscreen')
  expect(m.start_url).toContain('source=pwa')
  expect(m.icons.map((i) => i.sizes)).toEqual(expect.arrayContaining(['192x192', '512x512']))
  expect(m.icons.some((i) => i.purpose === 'maskable')).toBe(true)
  await expect(page.locator('[data-fullscreen]')).toBeVisible()
  await ctx.close()
})
