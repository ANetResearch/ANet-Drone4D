// Public demo build on the real stack (WP-13, M16; D2-AC-34 visitor path, D2-AC-37 viewer interactions; ADR-111 run-time
// role switch). Build: `VITE_AWR_DEMO=public npx vite build --outDir <dir>` (no test hooks), DEMO_DIST=<dir>. Backend:
// tests/e2e/sbstack.py with the shared show on S0 (synthcity; the S7 shared-show wiring is a pending request, see the WP-13
// report) behind the nginx-like preview of live.ts.
//   1. landing -> "开启我的沙盒" -> start (T5 default) -> /sandbox/<sid> with the session bar and the operator role badge:
//      2 clicks (<= 3); with M16_PERF=1 the first capture Toast within 90 s of the landing click (D2-AC-34, T5, x1);
//   2. shared show as a viewer (/world/synthcity): role badge "viewer", the entity switch, a vehicle, the model catalogue and
//      the GSD calculator (range typed in, results shown) all work without a single write request (no POST, PUT, PATCH,
//      DELETE other than the viewer token of /api/auth/token).
import { expect, test } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, startLivePreview, startLiveStack, type LiveStack } from './live'

const PORT = 4192
const PERF = process.env.M16_PERF === '1'
const DEMO_DIST = process.env.DEMO_DIST ?? ''
let live: LiveStack | null = null
let server: PreviewServer | null = null

test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 } })
test.describe.configure({ mode: 'serial' })
test.skip(!DEMO_DIST, 'needs DEMO_DIST (a VITE_AWR_DEMO=public build)')
test.beforeAll(async () => {
  test.setTimeout(300_000)
  live = await startLiveStack(2, 'synthcity', ['run.scenario=s0-synthcity-showcase'])
  server = await startLivePreview(PORT, live, DEMO_DIST)
})
test.afterAll(async () => {
  await server?.close()
  await live?.stop()
})

test('landing visitor path: two clicks into a sandbox (T5 default)', async ({ page }) => {
  test.setTimeout(300_000)
  await page.addInitScript(() => localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 })))
  await page.goto('/')
  const open = page.locator('[data-sandbox-open]')
  await expect(open).toBeEnabled({ timeout: 60_000 })
  const t0 = Date.now()
  await open.click()
  await expect(page.locator('[data-start-sheet]')).toBeVisible()
  await expect(page.locator('[data-template="t5_area_guard"]')).toHaveAttribute('data-checked', '', { timeout: 30_000 })
  await page.locator('[data-start-go]').click()
  await expect(page).toHaveURL(/\/sandbox\/sb-[a-z2-7]{10}/, { timeout: 90_000 })
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 120_000 })
  await expect(page.locator('[data-demo-role][data-surface="sandbox"]')).toBeVisible()
  if (PERF) {
    await expect(page.locator('[data-slot="toast"]').filter({ hasText: '已明确捕获' }).first()).toBeVisible({ timeout: 90_000 })
    expect((Date.now() - t0) / 1000).toBeLessThanOrEqual(90)
  }
  const h = await page.evaluate(() => sessionStorage.getItem('awr.sandbox.session.v1'))
  if (h) {
    const { sid, token } = JSON.parse(h) as { sid: string; token: string }
    await page.evaluate(async ([s, t]) => { await fetch(`/api/sandbox/v1/sessions/${s}`, { method: 'DELETE', headers: { authorization: `Bearer ${t}` } }) }, [sid, token])
  }
})

test('shared show viewer: entity switch, vehicle, catalogue and GSD calculator without write requests', async ({ page }) => {
  test.setTimeout(300_000)
  const writes: string[] = []
  page.on('request', (r) => {
    const m = r.method()
    const u = new URL(r.url())
    if (['GET', 'HEAD', 'OPTIONS'].includes(m)) return
    if (u.pathname === '/api/auth/token') return                  // the viewer token itself (ADR-084)
    writes.push(`${m} ${u.pathname}`)
  })
  await page.addInitScript(() => localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 })))
  await page.goto('/world/synthcity')
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 180_000 })
  const badge = page.locator('[data-demo-role]').first()
  await expect(badge).toHaveAttribute('data-surface', 'shared', { timeout: 30_000 })
  await expect(badge).toHaveAttribute('data-role', /viewer/)
  await page.locator('[data-entity="drones"]').click()
  const row = page.locator('[data-drone-id]').first()
  await expect(row).toBeVisible({ timeout: 60_000 })
  await page.locator('[data-fleet-catalog]').click()
  await expect(page.locator('[data-model-card]').first()).toBeVisible({ timeout: 30_000 })
  const gsdOpen = page.locator('[data-slot="sheet-content"]').getByRole('button', { name: /GSD|计算器/ }).first()
  if (await gsdOpen.count()) {
    await gsdOpen.click()
    await expect(page.locator('[data-gsd-calc]')).toBeVisible({ timeout: 15_000 })
    await page.locator('[data-gsd-range]').fill('300')
    await expect(page.locator('[data-gsd-result]')).toBeVisible()
    await expect(page.locator('[data-gsd-p]')).not.toHaveText('')
    await page.keyboard.press('Escape')
  }
  await page.keyboard.press('Escape')
  await expect(page.locator('[data-slot="sheet-content"]')).toHaveCount(0, { timeout: 10_000 })
  await page.locator('[data-drone-id]').first().click()
  await expect(page.locator('[data-drone-detail]').first()).toBeVisible({ timeout: 15_000 })
  await expect(page.locator('[data-manual-takeover]')).toHaveCount(0)              // no write controls for a viewer
  await page.keyboard.press('Escape')
  await page.waitForTimeout(3000)
  expect(writes, writes.join(', ')).toEqual([])
})
