// M15-S smoke (M15 §9.6 item 5: `npx playwright test perf/m15/smoke.spec.ts`): the shell boots on SwiftShader C1 without
// page errors, the canvas stays resident and never resizes while rails and the Dock open and close (M15-AC-001, AC-010
// in spirit), `/` lands on the default world (AC-003), below 1280 x 720 the full-viewport Empty replaces the shell
// (FR-007), the design sample renders every component family, and the rendered DOM holds no D1-AC-20 glyph and no
// backdrop filter. Screenshots go to .cache/impl/shots/ (AWR_SHOTS_DIR overrides) for the M15-S report.
// Needs a test build (VITE_AWR_TEST_SWITCHES=1 npm run build) for /dev/design and window.__ux; no backend is required
// (world list and realtime calls fail and show their empty and error states).
// The spec serves dist/ itself with `vite preview` on port 4183 (+10 x AWR_PORT_OFFSET), COOP/COEP on and only /api
// proxied: the shared vite.config.ts also proxies /assets and /worlds, which shadows the built bundle under /assets and
// the /worlds route in preview (request .cache/impl/requests/M15-to-M00.md); switch back to the configured webServer
// once that is fixed.
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { preview, type PreviewServer } from 'vite'

const SHOTS = process.env.AWR_SHOTS_DIR ?? join(import.meta.dirname, '..', '..', '..', '..', '.cache', 'impl', 'shots')
mkdirSync(SHOTS, { recursive: true })

const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0
const PORT = 4183 + 10 * OFFSET
let server: PreviewServer | null = null

test.use({ baseURL: `http://127.0.0.1:${PORT}` })
test.beforeAll(async () => {
  server = await preview({
    configFile: false,
    root: join(import.meta.dirname, '..', '..'),
    logLevel: 'silent', // proxy errors of the absent backend are expected
    build: { outDir: process.env.M15_DIST ?? 'dist' }, // M15_DIST: a private build output (parallel agents share dist/)
    preview: {
      host: '127.0.0.1',
      port: PORT,
      strictPort: true,
      headers: { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp' },
      proxy: { '/api': { target: `http://127.0.0.1:${8000 + 10 * OFFSET}`, ws: true } },
    },
  })
})
test.afterAll(async () => {
  await server?.close()
})

interface Watch { errors: string[]; console: string[] }
function watch(page: Page): Watch {
  const w: Watch = { errors: [], console: [] }
  page.on('pageerror', (e) => w.errors.push(`${e.name}: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    w.console.push(m.text())
    // error boundaries (M15-E004 root, viewport and panel) catch render errors before they become page errors
    if (/M15-E0\d\d|TypeError|ReferenceError|RangeError/.test(m.text())) w.errors.push(m.text().split('\n')[0])
  })
  return w
}

async function shot(page: Page, name: string, fullPage = false): Promise<void> {
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage, animations: 'disabled' })
}

async function revealed(page: Page): Promise<void> {
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 30_000 })
  await expect(page.locator('[data-figure="header"]')).toBeVisible()
}

/** D1-AC-20 in the rendered DOM: no emoji, no geometric shapes, misc symbols, dingbats or arrows from U+2194 */
async function glyphScan(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const re = /[\p{Extended_Pictographic}\p{Emoji_Presentation}\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]|\u{FE0F}/u
    const bad: string[] = []
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const s = n.textContent ?? ''
      if (re.test(s)) bad.push(s.slice(0, 40))
    }
    for (const el of document.querySelectorAll('[aria-label],[title],[placeholder]')) {
      for (const a of ['aria-label', 'title', 'placeholder']) {
        const v = el.getAttribute(a)
        if (v && re.test(v)) bad.push(`${a}=${v.slice(0, 40)}`)
      }
    }
    return bad
  })
}

async function backdropFilters(page: Page): Promise<number> {
  return page.evaluate(() => {
    let n = 0
    for (const el of document.querySelectorAll('*')) {
      const v = getComputedStyle(el).backdropFilter
      if (v && v !== 'none') n++
    }
    return n
  })
}

async function canvasBuffer(page: Page): Promise<string> {
  return page.evaluate(() => {
    const c = document.querySelector<HTMLCanvasElement>('[data-viewport] canvas')
    return c ? `${c.width}x${c.height}` : 'none'
  })
}

test.describe('M15-S shell smoke', () => {
  test('root redirects to the default world within 4 s', async ({ page }) => {
    const w = watch(page)
    await page.goto('/')
    await page.waitForURL(/\/world\/[a-z0-9-]+/, { timeout: 4_000 })
    expect(w.errors).toEqual([])
  })

  test('sandbox at 1280 x 720 and 1920 x 1080', async ({ page }) => {
    const w = watch(page)
    await page.goto('/world/shenzhen')
    await revealed(page)
    await expect(page.locator('[data-viewport] canvas')).toHaveCount(1)
    await page.waitForTimeout(600)
    await shot(page, 'm15-world-1280x720')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const header = await page.locator('[data-figure="header"]').boundingBox()
    expect(header?.height).toBe(44)

    await page.setViewportSize({ width: 1920, height: 1080 })
    await page.waitForTimeout(600)
    await shot(page, 'm15-world-1920x1080')
    expect(await glyphScan(page)).toEqual([])
    expect(await backdropFilters(page)).toBe(0)
    expect(w.errors).toEqual([])
  })

  test('rails and dock float over a canvas that never resizes', async ({ page }) => {
    const w = watch(page)
    await page.setViewportSize({ width: 1920, height: 1080 })
    await page.goto('/world/shenzhen')
    await revealed(page)
    await page.waitForTimeout(300)
    const before = await canvasBuffer(page)
    expect(before).not.toBe('none')
    await page.locator('body').click({ position: { x: 960, y: 500 } })
    for (const key of ['Backquote', 'Backslash', 'Control+KeyB']) {
      await page.keyboard.press(key)
      await page.waitForTimeout(400)
      expect(await canvasBuffer(page)).toBe(before)
    }
    await shot(page, 'm15-world-dock-open-rails-closed')
    for (const key of ['Backquote', 'Backslash', 'Control+KeyB']) {
      await page.keyboard.press(key)
      await page.waitForTimeout(400)
      expect(await canvasBuffer(page)).toBe(before)
    }
    const commits = await page.evaluate(() => (window as unknown as { __ux?: { layout?: { unobscuredCommits?: number } } }).__ux?.layout?.unobscuredCommits ?? -1)
    expect(commits).not.toBe(0)
    expect(w.errors).toEqual([])
  })

  test('settings deep link, command palette and shortcut help', async ({ page }) => {
    const w = watch(page)
    // modal content stays mounted until its exit transition ends; SwiftShader frames are slow right after boot
    const modal = page.locator('[data-slot="dialog-content"]')
    await page.goto('/world/shenzhen?settings=motion')
    await revealed(page)
    await expect(modal).toBeVisible()
    await page.waitForTimeout(400)
    await shot(page, 'm15-settings-motion')
    await page.keyboard.press('Escape')
    await expect(modal).toHaveCount(0, { timeout: 15_000 })
    expect(new URL(page.url()).searchParams.has('settings')).toBe(false)
    await page.keyboard.press('Control+KeyK')
    await expect(modal).toBeVisible()
    await page.waitForTimeout(400)
    await shot(page, 'm15-command-palette')
    await page.keyboard.press('Escape')
    await expect(modal).toHaveCount(0, { timeout: 15_000 })
    await page.keyboard.press('Shift+Slash')
    await expect(modal).toBeVisible()
    await page.waitForTimeout(400)
    await shot(page, 'm15-shortcut-help')
    expect(await glyphScan(page)).toEqual([])
    expect(w.errors).toEqual([])
  })

  test('world hub over the resident canvas', async ({ page }) => {
    const w = watch(page)
    await page.goto('/world/shenzhen')
    await revealed(page)
    const before = await canvasBuffer(page)
    await page.evaluate(() => {
      history.pushState(null, '', '/worlds')
      dispatchEvent(new PopStateEvent('popstate'))
    })
    await expect(page.locator('[data-view="world-hub"]')).toBeVisible()
    await page.waitForTimeout(600)
    await shot(page, 'm15-world-hub')
    expect(await canvasBuffer(page)).toBe(before)
    expect(w.errors).toEqual([])
  })

  test('canvas-only mode and the small-window empty state', async ({ page }) => {
    const w = watch(page)
    await page.goto('/world/shenzhen?chrome=0')
    await expect(page.locator('[data-viewport] canvas')).toHaveCount(1)
    await expect(page.locator('[data-figure="header"]')).toHaveCount(0)
    await page.setViewportSize({ width: 1100, height: 640 })
    await page.goto('/world/shenzhen')
    await expect(page.locator('[data-empty="full"]')).toBeVisible({ timeout: 30_000 })
    await page.waitForTimeout(400)
    await shot(page, 'm15-small-window')
    expect(w.errors).toEqual([])
  })

  test('design sample (test build)', async ({ page }) => {
    const w = watch(page)
    const res = await page.goto('/dev/design')
    expect(res?.ok()).toBe(true)
    const sample = page.locator('[data-view="design"]')
    test.skip((await sample.count()) === 0 && !(await page.evaluate(() => '__ux' in window)), 'production build: /dev/design is test-only')
    await expect(sample).toBeVisible({ timeout: 30_000 })
    await page.setViewportSize({ width: 1600, height: 1000 })
    await page.waitForTimeout(800)
    await shot(page, 'm15-design-sample')
    // the sample is a fixed overlay page that scrolls inside; grow the viewport to capture all of it
    const tall = await sample.evaluate((el) => Math.min(6000, Math.ceil(el.scrollHeight)))
    await page.setViewportSize({ width: 1600, height: Math.max(1000, tall) })
    await page.waitForTimeout(1200)
    await shot(page, 'm15-design-sample-full')
    expect(await glyphScan(page)).toEqual([])
    expect(await backdropFilters(page)).toBe(0)
    expect(w.errors).toEqual([])
  })
})
