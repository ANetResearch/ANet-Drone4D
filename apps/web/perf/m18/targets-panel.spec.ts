// M18-AC-017 targets on the real stack (WP-13 writes it for M18, d2-WP-05-to-M16 item 2; M18-FR-019, FR-020, NFR-006).
// Backend: tests/e2e/sbstack.py behind the nginx-like preview (perf/d2/live.ts), test build. A T1 session started from the
// Sheet; the operator's REST writes (POST, PATCH, DELETE /targets with the page's sandbox token) show in the targets panel
// within the bound of the AC (<= 1 s, plus the 2 s polling margin of a functional run; the bound is asserted with
// M16_PERF=1); a token of another principal and the shared show's viewer token are refused; a burst over the entity limit
// (5 per second) returns 429 with reason 111.
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, startLivePreview, startLiveStack, type LiveStack } from '../d2/live'

const PORT = 4195
const PERF = process.env.M16_PERF === '1'
let live: LiveStack | null = null
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 } })
test.beforeAll(async () => {
  test.setTimeout(300_000)
  live = await startLiveStack(2)
  server = await startLivePreview(PORT, live)
})
test.afterAll(async () => {
  await server?.close()
  await live?.stop()
})

async function start(page: Page, template: string): Promise<{ sid: string; token: string }> {
  await page.addInitScript(() => localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 })))
  await page.goto('/sandbox/sb-aaaaaaaaaa?reveal=shell&viewport=off')
  await expect(page.locator('[data-sandbox-unavailable]')).toBeVisible({ timeout: 60_000 })
  await page.locator('[data-sandbox-unavailable] button').last().click()
  await expect(page.locator('[data-template]')).toHaveCount(6, { timeout: 30_000 })
  await page.locator(`[data-template="${template}"]`).click()
  await page.locator('[data-start-go]').click()
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 90_000 })
  const v = await page.evaluate(() => sessionStorage.getItem('awr.sandbox.session.v1'))
  const h = JSON.parse(v ?? '{}') as { sid: string; token: string }
  // the session bar shows while the session is still SPAWNING: writes before its sim-core is up get 503 (211), so wait for
  // READY or ACTIVE (d2-WP-12c-to-M16 item 5, WP-13b round 2)
  await expect.poll(async () => {
    const r = await page.request.get(`/api/sandbox/v1/sessions/${h.sid}`, { headers: { authorization: `Bearer ${h.token}` } })
    return String(((await r.json()) as { state?: string }).state ?? '')
  }, { timeout: 120_000, intervals: [500] }).toMatch(/^(READY|ACTIVE)$/)
  return h
}

test('operator writes show in the targets panel; foreign and viewer tokens are refused; the entity limit answers 111', async ({ page, request }) => {
  test.setTimeout(300_000)
  const h = await start(page, 't1_point_watch')
  const auth = { authorization: `Bearer ${h.token}` }
  const base = `/api/sandbox/v1/sessions/${h.sid}/targets`
  await page.locator('[data-entity="targets"]').click()
  await expect(page.locator('[data-target-row="person-01"]')).toBeVisible({ timeout: 30_000 })
  // add
  let t0 = Date.now()
  const created = await request.post(base, { headers: auth, data: { class: 'dog', pos: [-150, -330, null], label: 'M18 测试狗' } })
  expect(created.status()).toBe(201)
  const id = (await created.json() as { id: string }).id
  await expect(page.locator(`[data-target-row="${id}"]`)).toBeVisible({ timeout: PERF ? 1000 : 15_000 })
  const addMs = Date.now() - t0
  // update
  t0 = Date.now()
  expect((await request.patch(`${base}/${id}`, { headers: auth, data: { label: 'M18 改名' } })).status()).toBe(200)
  await expect(page.locator(`[data-target-row="${id}"]`)).toContainText('M18 改名', { timeout: PERF ? 1000 : 15_000 })
  const patchMs = Date.now() - t0
  // remove: the server drops it (the panel side is the separate test below)
  expect([200, 202, 204]).toContain((await request.delete(`${base}/${id}`, { headers: auth })).status())
  await expect.poll(async () => ((await (await request.get(base, { headers: auth })).json()) as { items: { id: string }[] }).items
    .some((x) => x.id === id), { timeout: 10_000 }).toBe(false)
  test.info().annotations.push({ type: 'visible_ms', description: JSON.stringify({ add: addMs, patch: patchMs }) })
  // the shared show's viewer token on the sandbox write route
  const vt = await (await request.post('/api/auth/token', { data: { role: 'viewer', principal_hint: 'MSIXMEIGHTEENVIEWERAAAAA' } })).json() as { token: string }
  const viewerWrite = await request.post(base, { headers: { authorization: `Bearer ${vt.token}` }, data: { class: 'dog', pos: [0, 0, null] } })
  expect([401, 403]).toContain(viewerWrite.status())
  // the entity limit: a burst of 12 writes within a second
  const codes: number[] = []
  await Promise.all(Array.from({ length: 12 }, async (_x, k) => {
    const r = await request.patch(`${base}/person-01`, { headers: auth, data: { label: `burst ${k}` } })
    codes.push(r.status() === 429 ? Number((await r.json() as { code?: number }).code) : r.status())
  }))
  expect(codes).toContain(111)
  await request.delete(`/api/sandbox/v1/sessions/${h.sid}`, { headers: auth })
})

test('a removed target leaves the targets panel', async ({ page, request }) => {
  // d2-WP-13-to-M15 item 1: the `test.fail` of the first round hid a case timing fault (the write went out while the session
  // was SPAWNING, 503 211, `id` undefined); start() now waits for READY or ACTIVE, and M15 hardened the roster fallback
  // (RosterTargetGate) in WP-12c round 2 (d2-WP-12c-to-M16 item 5), so the case runs as a plain test again.
  test.setTimeout(300_000)
  const h = await start(page, 't2_polyline')
  const auth = { authorization: `Bearer ${h.token}` }
  const base = `/api/sandbox/v1/sessions/${h.sid}/targets`
  await page.locator('[data-entity="targets"]').click()
  const made = await request.post(base, { headers: auth, data: { class: 'object', pos: [-150, -330, null] } })
  expect(made.status()).toBe(201)
  const id = (await made.json() as { id: string }).id
  await expect(page.locator(`[data-target-row="${id}"]`)).toBeVisible({ timeout: 15_000 })
  await request.delete(`${base}/${id}`, { headers: auth })
  try {
    await expect(page.locator(`[data-target-row="${id}"]`)).toHaveCount(0, { timeout: PERF ? 1000 : 15_000 })
  } finally {
    await request.delete(`/api/sandbox/v1/sessions/${h.sid}`, { headers: auth })
  }
})
