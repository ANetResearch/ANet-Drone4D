// D2 visitor path, OpenAPI page and manual control on the real sandbox stack (WP-13, M16; D2-AC-34, D2-AC-22 page part,
// D2-AC-21 setpoint part, M15-AC-211). Backend: tests/e2e/sbstack.py (supervisor `sandbox` profile; the pool spawns a real
// sim-core per session from scenarios/sandbox/<world>/<template>.json) behind a private preview routed like nginx (live.ts).
//   1. visitor path: a sandbox link the browser does not hold -> "unavailable" page -> start Sheet (worlds, T1-T6, T5 default)
//      -> T1 -> start -> /sandbox/<sid>; the session bar shows, the first `perception.capture` Toast ("已明确捕获") arrives;
//      clicks counted from the page (<= 3 including the template choice). The 90 s bound of D2-AC-34 (T5, x1) is a harness
//      timing only asserted with M16_PERF=1; functional runs use T1 (about 40 s simulated) and a 240 s ceiling;
//   2. /sandbox/api renders every operation of the live sandbox-api OpenAPI document, no token in the page;
//   3. manual control (T6, the P600 hovering by the target): takeover confirmation, W held 2 s -> setpoints >= 25 Hz
//      (window.__ux.sandbox.setpoints), the rate control is locked while held, release returns the vehicle.
// Build: `D2_DIST=<dir> node perf/m15/helpers/build-ui.mjs` (VITE_AWR_TEST_SWITCHES=1); run:
// `D2_DIST=<dir> npx playwright test perf/d2/sandbox-live.spec.ts --project perf` (AWR_PORT_OFFSET for private ports).
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, countOps, startLivePreview, startLiveStack, type LiveStack } from './live'

const PORT = 4191
const PERF = process.env.M16_PERF === '1'
let live: LiveStack | null = null
let server: PreviewServer | null = null

test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 } })
test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => {
  test.setTimeout(300_000)
  live = await startLiveStack(2)
  server = await startLivePreview(PORT, live)
})
test.afterAll(async () => {
  await server?.close()
  await live?.stop()
})

interface UxSandbox { setpoints: number; surface: string; phase: string }
const uxSandbox = (page: Page): Promise<UxSandbox> => page.evaluate(() => {
  const s = (window as unknown as { __ux: { sandbox: UxSandbox } }).__ux.sandbox
  return { setpoints: s.setpoints, surface: s.surface, phase: s.phase }
})

async function skipOnboarding(page: Page): Promise<void> {
  await page.addInitScript(() => localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 })))
}

/** sid and token of the session this page holds (sessionStorage, AWR-14 §20.4.7) */
async function held(page: Page): Promise<{ sid: string; token: string } | null> {
  return page.evaluate(() => {
    const v = sessionStorage.getItem('awr.sandbox.session.v1')
    return v ? (JSON.parse(v) as { sid: string; token: string }) : null
  })
}

async function endSession(page: Page): Promise<void> {
  const h = await held(page).catch(() => null)
  if (!h) return
  await page.evaluate(async ({ sid, token }) => {
    await fetch(`/api/sandbox/v1/sessions/${sid}`, { method: 'DELETE', headers: { authorization: `Bearer ${token}` } })
  }, h)
}

async function openFromSheet(page: Page, template: string, query = ''): Promise<{ clicks: number; t0: number }> {
  let clicks = 0
  await page.goto(`/sandbox/sb-aaaaaaaaaa?reveal=shell${query}`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 60_000 })
  await expect(page.locator('[data-sandbox-unavailable]')).toBeVisible({ timeout: 30_000 })
  const t0 = Date.now()
  await page.locator('[data-sandbox-unavailable] button').last().click()
  clicks++
  await expect(page.locator('[data-start-sheet]')).toBeVisible()
  await expect(page.locator('[data-template]')).toHaveCount(6, { timeout: 30_000 })
  await expect(page.locator('[data-template="t5_area_guard"]')).toHaveAttribute('data-checked', '')
  if (template !== 't5_area_guard') {
    await page.locator(`[data-template="${template}"]`).click()
    clicks++
  }
  await page.locator('[data-start-go]').click()
  clicks++
  await expect(page).toHaveURL(/\/sandbox\/sb-[a-z2-7]{10}/, { timeout: 60_000 })
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
  return { clicks, t0 }
}

test('visitor path: start a sandbox from the Sheet and see the first capture Toast', async ({ page }) => {
  test.setTimeout(420_000)
  await skipOnboarding(page)
  const template = PERF ? 't5_area_guard' : 't1_point_watch'
  const { clicks, t0 } = await openFromSheet(page, template)
  expect(clicks).toBeLessThanOrEqual(3)
  await expect(page.locator('[data-session-badge]')).toContainText('synthcity')
  const toast = page.locator('[data-slot="toast"]').filter({ hasText: '已明确捕获' })
  await expect(toast.first()).toBeVisible({ timeout: PERF ? 90_000 : 240_000 })
  const s = (Date.now() - t0) / 1000
  test.info().annotations.push({ type: 'first_capture_wall_s', description: s.toFixed(1) })
  if (PERF) expect(s).toBeLessThanOrEqual(90)
  expect((await uxSandbox(page)).surface).toBe('sandbox')
  await endSession(page)
})

test('/sandbox/api lists every operation of the live document without a token', async ({ page, request }) => {
  const doc = await (await request.get('/api/sandbox/v1/openapi.json')).json() as { paths: Record<string, Record<string, unknown>> }
  const ops = countOps(doc)
  expect(ops).toBeGreaterThanOrEqual(70)
  await page.goto('/sandbox/api?reveal=shell&viewport=off')
  await expect(page.locator('[data-view="api-docs"]')).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('[data-api-nav]')).toHaveAttribute('data-api-nav', String(ops), { timeout: 30_000 })
  expect(await page.locator('body').innerText()).not.toMatch(/v1\.[A-Za-z0-9_-]{20,}\./)
})

test('manual control: takeover, setpoints at >= 25 Hz, rate locked, release', async ({ page, request }) => {
  test.setTimeout(420_000)
  await skipOnboarding(page)
  await openFromSheet(page, 't6_manual', '&viewport=off')   // UI-only: the realtime link and the pump, no 3D on SwiftShader
  const h = await held(page)
  expect(h).not.toBeNull()
  // the T6 P600 takes off on its point_watch task; wait until it is flying (REST state, outside the page: the sandbox route
  // may reload once to follow the session world)
  await expect.poll(async () => {
    const r = await request.get(`/api/sandbox/v1/sessions/${h!.sid}/vehicles/p600-01/state`, { headers: { authorization: `Bearer ${h!.token}` } })
    const j = await r.json() as { state?: { flight_state?: string } }
    return j.state?.flight_state ?? ''
  }, { timeout: 150_000, intervals: [2000] }).toBe('FLYING')
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
  await page.locator('[data-entity="drones"]').click()
  await page.locator('[data-drone-id="p600-01"]').first().click()
  await page.locator('[data-manual-takeover="p600-01"]').click()
  await expect(page.locator('[data-manual-confirm]')).toBeVisible()
  await page.locator('[data-manual-confirm-go]').click()
  await expect(page.locator('[data-manual-bar]')).toBeVisible({ timeout: 30_000 })
  const n0 = (await uxSandbox(page)).setpoints
  const t0 = Date.now()
  await page.keyboard.down('KeyW')
  await page.waitForTimeout(2000)
  await page.keyboard.up('KeyW')
  const hz = ((await uxSandbox(page)).setpoints - n0) / ((Date.now() - t0) / 1000)
  test.info().annotations.push({ type: 'setpoint_hz', description: hz.toFixed(1) })
  expect(hz).toBeGreaterThanOrEqual(25)
  // rates above x1 carry the reason manual.rateLocked while the takeover is held (timelineGuards, D2-AC-21)
  const locked = page.locator('[data-rate-toggle] button[disabled], [data-rate-toggle] [data-disabled]')
  expect(await locked.count()).toBeGreaterThan(0)
  await page.locator('[data-manual-release]').click()
  await expect(page.locator('[data-manual-bar]')).toHaveCount(0, { timeout: 30_000 })
  await endSession(page)
})
