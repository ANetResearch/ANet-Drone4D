// Live viewer seats full on the real stack (WP-13b round 2, M16; D2-AC-43 viewer side, D2-AC-44 resume with a queue,
// D2-AC-41 "观看满" line; R-D2-31, R-D2-32, R-D2-34; AWR-04 §15.6.3, §15.6.5, §15.7.3). Backend: perf/d21/live21.ts
// (tests/e2e/sbstack.py with `viewer_seats.max = 1` and the power thresholds of power-live), the test build (D21_DIST).
// The first round could only inject 4431 (M15 d21-shell); since WP-03b (D2-INT) show-api counts the seats, refuses the
// handshake with error 517 + close 4431 and runs the R81 to R83 queue, so the whole path runs for real here:
//   1. page A (shared show viewer, `?power=viewer` with the test thresholds) holds the only seat, goes to power saving: the seat
//      is free (show-api live 0);
//   2. page B (another visitor, `?power=off`, UI only) takes the seat: capacity `viewers` 1 / 1, state full;
//   3. any key on A: RESUMING -> 4431 -> QUEUED with the viewer queue card "实时观看排队中 · 第 1 位" (static browsing);
//      the capacity endpoint counts the queue;
//   4. a page C loading meanwhile shows "实时观看已满（1 / 1）· 进入后排队，第 2 位" and the "需排队" badge on the boot
//      line, before the reveal;
//   5. B leaves: A's ticket turns seat_ready and A connects by itself (no click): ACTIVE, the link LIVE, the card gone, show-api
//      live 1; the time from B leaving to A connected is recorded (D2-AC-43 "<= 5 s" asserted with M16_PERF=1, functional
//      bound 15 s).
// Found by this case (round 2): the page queued with a principal hint show-api did not take, so step 5 never happened on the
// real stack (d2-WP-13b-to-M15 item 10). Fixed in D2-INT (one shared hint, both servers take both forms): the case asserts
// that the ticket's hint is the page token's hint.
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { emulate, MIN } from './limits'
import {
  baseUrl, capacity, ensureBuild, isTestBuild, PERF, revealed, shot, showClients, skipOnboarding, startLive21, startLive21Preview,
  watch, type Live21,
} from './live21'

const PORT = 4202
const POWER = { viewer_idle_s: 20, viewer_idle_busy_s: 20, sandbox_idle_s: 20, sandbox_idle_busy_s: 20, warn_s: 5, hidden_s: 4, quiet_s: 8, grace_s: 10 }
const Q = 'powerIdle=20&powerWarn=5&powerQuiet=8&powerHidden=4'
let live: Live21 | null = null
let server: PreviewServer | null = null

test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 }, locale: 'zh-CN' })
test.beforeAll(async () => {
  test.setTimeout(600_000)
  const dist = ensureBuild('test')
  live = await startLive21({ maxSessions: 1, power: POWER, releaseFrom: dist, set: ['viewer_seats.max=1'] })
  server = await startLive21Preview(PORT, live, dist)
})
test.afterAll(async () => {
  await server?.close()
  await live?.stop()
})

type Ux = { __ux: { power: { state: string; transitions: string[]; conn?: string } } }
const powerState = (page: Page): Promise<string> => page.evaluate(() => (window as unknown as Ux).__ux.power.state)
const conn = (page: Page): Promise<string> => page.evaluate(() => String((window as unknown as Ux).__ux.power.conn ?? ''))
const viewers = async (): Promise<{ live: number; max: number; queue: number; state: string }> =>
  (await capacity(live!)).viewers as unknown as { live: number; max: number; queue: number; state: string }

test('seats full: saving frees the seat, resume queues behind another viewer, seat_ready reconnects by itself', async ({ page, browser }) => {
  test.setTimeout(420_000)
  const w = watch(page)
  // principals: the page token's hint (R01 answer) and the hint the page queues with (R81 body), for the failure analysis
  const hints: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/api/viewer/queue') && r.method() === 'POST') hints.push(`R81 ${String((r.postDataJSON() as { principal_hint?: string } | null)?.principal_hint)}`)
  })
  page.on('response', async (r) => {
    if (r.url().endsWith('/api/auth/token') && r.status() === 200) hints.push(`R01 ${String(((await r.json().catch(() => ({}))) as { principal_hint?: string }).principal_hint)}`)
  })
  await skipOnboarding(page)
  // page A renders under the WebGL2 minimum limits (ADR-086, ADR-087): saving, queued static browsing and the resume add no
  // GL error or refused binding (D2-AC-54 for the seat-queue states)
  await page.addInitScript(emulate, { L: MIN, renderer: null, mobile: null })
  expect((await showClients(live!)).n).toBe(0)
  // 1. A holds the only seat, then saves
  await page.goto(`/world/synthcity?power=viewer&${Q}`)
  await revealed(page)
  test.skip(!(await isTestBuild(page)), 'needs a test build')
  await expect.poll(() => conn(page), { timeout: 60_000 }).toMatch(/^LIVE/)
  await expect.poll(async () => (await viewers()).live, { timeout: 10_000 }).toBe(1)
  expect((await viewers()).max).toBe(1)
  await page.mouse.move(600, 400)
  await page.mouse.move(820, 460, { steps: 5 })
  await expect(page.locator('[data-power-overlay="SAVING"]')).toBeVisible({ timeout: 40_000 })
  await expect.poll(async () => (await showClients(live!)).n, { timeout: 5_000, intervals: [250] }).toBe(0)

  // 2. B takes the seat
  const bctx = await browser.newContext({ baseURL: baseUrl(PORT), viewport: { width: 1280, height: 800 }, locale: 'zh-CN' })
  const b = await bctx.newPage()
  await skipOnboarding(b)
  await b.goto('/world/synthcity?power=off&reveal=shell&viewport=off')
  await revealed(b)
  await expect.poll(async () => (await viewers()).live, { timeout: 30_000 }).toBe(1)
  await expect.poll(async () => (await viewers()).state, { timeout: 5_000 }).toBe('full')

  // 3. any key on A: refused (4431), the viewer queue card, the queue counted by the capacity endpoint
  await page.keyboard.press('KeyX')
  const card = page.locator('[data-queue-kind="viewer"]')
  await expect(card).toBeVisible({ timeout: 30_000 })
  await expect(card).toContainText('实时观看排队中 · 第 1 位')
  expect(await powerState(page)).toBe('QUEUED')
  await expect.poll(async () => (await viewers()).queue, { timeout: 5_000 }).toBe(1)
  await shot(page, 'd21-seats-live-viewer-queue')
  // the ticket must name the page token's principal (d2-WP-13b-to-M15 item 10, fixed in D2-INT): otherwise the seat reserved
  // on seat_ready never matches the page's realtime token and the page stays QUEUED
  // saving and queued static browsing under the minimum limits: no GL error so far
  await page.mouse.move(640, 420)
  await page.mouse.down()
  await page.mouse.move(700, 440, { steps: 4 })
  await page.mouse.up()
  await page.waitForTimeout(500)
  expect(w.gl).toEqual([])
  const sent = (k: string): string[] => hints.filter((x) => x.startsWith(`${k} `)).map((x) => x.slice(4))
  await expect.poll(() => sent('R81').length, { timeout: 10_000 }).toBeGreaterThan(0)
  const foreign = sent('R81').filter((h) => !sent('R01').includes(h))
  test.info().annotations.push({ type: 'queue_principal', description: foreign.length ? `R81 hint not a token hint: ${foreign.join(',')}` : 'R81 hint = token hint' })
  expect(foreign, 'viewer queue ticket for another principal than the page token (d2-WP-13b-to-M15 item 10)').toEqual([])

  // 4. a visitor loading now sees the full seats and its place on the boot line
  const cctx = await browser.newContext({ baseURL: baseUrl(PORT), viewport: { width: 1280, height: 800 }, locale: 'zh-CN' })
  const c = await cctx.newPage()
  await skipOnboarding(c)
  await c.goto('/world/synthcity?power=off')
  const line = c.locator('#boot-mask .boot-capacity')
  await expect(line).toHaveAttribute('data-capacity-hint', 'queueViewer', { timeout: 20_000 })
  await expect(line.locator('.boot-capacity-text')).toHaveText(/^实时观看已满（1 \/ 1）· 进入后排队，第 2 位，/)
  await expect(line.locator('[data-capacity-queue]')).toBeVisible()
  expect(await c.locator('#boot-mask').count()).toBe(1)                          // before the reveal (R-D2-32)
  await shot(c, 'd21-seats-live-boot-queue-viewer')
  await cctx.close()                                                              // C never connected: A stays first

  // 5. B leaves: A's ticket is served and A connects without a click
  const t0 = Date.now()
  await bctx.close()
  try {
    await expect.poll(() => powerState(page), { timeout: 45_000, intervals: [250] }).toBe('ACTIVE')
  } finally {
    test.info().annotations.push({ type: 'principal_hints', description: [...new Set(hints)].join(' | ') })
    test.info().annotations.push({ type: 'viewers_after_b_left', description: JSON.stringify(await viewers().catch(() => null)) })
    test.info().annotations.push({ type: 'gl_messages', description: JSON.stringify(w.gl.slice(0, 5)) })
  }
  const dt = Date.now() - t0
  test.info().annotations.push({ type: 'seat_ready_to_active_ms', description: String(dt) })
  await expect.poll(() => conn(page), { timeout: 15_000 }).toMatch(/^LIVE/)
  await expect(card).toHaveCount(0)
  await expect(page.locator('[data-power-overlay]')).toHaveCount(0)
  await expect.poll(async () => (await showClients(live!)).n, { timeout: 10_000 }).toBe(1)
  expect(dt).toBeLessThanOrEqual(PERF ? 5_000 : 15_000)
  const tr = await page.evaluate(() => (window as unknown as Ux).__ux.power.transitions)
  expect(tr.some((x) => x.startsWith('RESUMING>QUEUED'))).toBe(true)
  expect(tr.some((x) => x.startsWith('QUEUED>RESUMING'))).toBe(true)
  expect(w.gl).toEqual([])
  expect(w.errors).toEqual([])
})
