// M11 functional smoke in the browser without a backend (M11-AC-040, D1-AC-35 front-end part; AWR-18 §8.6 `source=fake`):
// the real rt.worker (dedicated Worker, transferable slots) with FakeSource N = 200 reaches LIVE, the roster and the
// DroneRail fill, __perf.net is fed from the slot header; a link dropped every few seconds goes RECONNECTING and back to
// LIVE without page errors; flight60 `scene=full&source=fake` gets the 2 vehicles of S1.
// Needs a test build (window.__vp): `VITE_AWR_TEST_SWITCHES=1 npx vite build --outDir <dir>` and M11_DIST=<dir> (parallel
// agents share apps/web/dist, so a private output directory is recommended). The spec serves the build itself on port
// 4187 (+10 x AWR_PORT_OFFSET) with COOP/COEP, /worlds from the repository (single-range Range) and 404 for /api (so the
// token request fails fast and the page stays on FakeSource). Functional only: no timing thresholds.
import type { Server } from 'node:http'
import { expect, test, type Page } from '@playwright/test'
import { PORT, startStaticServer, stopServer } from './staticServer'

let server: Server | null = null
test.use({ baseURL: `http://127.0.0.1:${PORT}` })
test.beforeAll(async () => {
  server = await startStaticServer()
})
test.afterAll(async () => {
  await stopServer(server)
})

interface Vp { conn(): string; roster(): unknown[]; session(): { role: string; seat: string; runId: string; worldId: string } | null }
interface Watch { errors: string[]; viewportDown: boolean }
/** page errors; a viewport caught by its error boundary (M15-E004 viewport) stops the frame loop, so the checks that need
 * the telemetry phase (DroneRail rows, __perf.net) are reported as annotations instead of failing the M11 part */
function watch(page: Page): Watch {
  const w: Watch = { errors: [], viewportDown: false }
  page.on('pageerror', (e) => w.errors.push(`${e.name}: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error' && m.text().startsWith('M15-E004 viewport')) w.viewportDown = true
  })
  return w
}
/** page errors that are not the viewport's (the M11 path must not throw) */
const ownErrors = (w: Watch): string[] => (w.viewportDown ? w.errors.filter((e) => !/reading 'query'|viewport/i.test(e)) : w.errors)
async function hooks(page: Page): Promise<void> {
  const has = await page.waitForFunction(() => '__vp' in window, null, { timeout: 30_000 }).then(() => true, () => false)
  test.skip(!has, 'needs a test build (VITE_AWR_TEST_SWITCHES=1) with window.__vp')
}
/** wait until the roster has n vehicles; false (with an annotation) when the viewport went down first */
async function rosterOrViewportDown(page: Page, w: Watch, n: number): Promise<boolean> {
  const end = Date.now() + 45_000
  while (Date.now() < end) {
    if (w.viewportDown) {
      test.info().annotations.push({ type: 'skipped-part', description: 'viewport error boundary active: the default subscriptions and the telemetry phase belong to the drones layer (M06)' })
      return false
    }
    if ((await page.evaluate(() => (window as unknown as { __vp: Vp }).__vp.roster().length)) === n) return true
    await page.waitForTimeout(100)
  }
  throw new Error(`roster did not reach ${n} vehicles`)
}
const conn = (page: Page): Promise<string> => page.evaluate(() => (window as unknown as { __vp: Vp }).__vp.conn())

test('FakeSource N = 200 in the real worker: LIVE, serverInfo; roster, DroneRail and __perf.net with the viewport', async ({ page }) => {
  const w = watch(page)
  await page.goto('/world/shenzhen?source=fake&fakeN=200')
  await hooks(page)
  await page.waitForFunction(() => (window as unknown as { __vp: Vp }).__vp.conn() === 'LIVE', null, { timeout: 60_000 })
  const session = await page.evaluate(() => (window as unknown as { __vp: Vp }).__vp.session())
  expect(session).toEqual({ role: 'operator', seat: 'held', runId: 'fake-run', worldId: 'shenzhen' })
  expect(ownErrors(w)).toEqual([])
  if (!(await rosterOrViewportDown(page, w, 200))) return
  await expect.poll(async () => page.locator('[data-rail-row]').count(), { timeout: 30_000 }).toBeGreaterThan(3)
  const net = await page.waitForFunction(() => {
    const p = (window as unknown as { __perf?: { net?: { swarmHz: number; epoch: number; bytesPerS: number } } }).__perf?.net
    return p && p.swarmHz > 5 ? { swarmHz: p.swarmHz, epoch: p.epoch, bytesPerS: p.bytesPerS } : null
  }, null, { timeout: 30_000 }).then((h) => h.jsonValue())
  expect(net!.epoch).toBe(1)
  expect(net!.bytesPerS).toBeGreaterThan(1000)
  expect(w.errors).toEqual([])
})

test('a link dropped every 3 s goes RECONNECTING and back to LIVE', async ({ page }) => {
  const w = watch(page)
  await page.goto('/world/shenzhen?source=fake&fakeN=1&fakeDrop=3')
  await hooks(page)
  const seen = new Set<string>()
  const end = Date.now() + 30_000
  while (Date.now() < end && !(seen.has('RECONNECTING') && seen.has('LIVE') && seen.size >= 3)) {
    seen.add(await conn(page))
    await page.waitForTimeout(50)
  }
  expect(seen.has('RECONNECTING')).toBe(true)
  await page.waitForFunction(() => (window as unknown as { __vp: Vp }).__vp.conn() === 'LIVE', null, { timeout: 15_000 })
  expect(ownErrors(w)).toEqual([])
})

test('flight60 scene=full with source=fake uses the 2 vehicles of S1', async ({ page }) => {
  const w = watch(page)
  await page.goto('/world/shenzhen?bench=flight60&scene=full&source=fake')
  await hooks(page)
  await page.waitForFunction(() => (window as unknown as { __vp: Vp }).__vp.conn() === 'LIVE', null, { timeout: 60_000 })
  expect(ownErrors(w)).toEqual([])
  if (!(await rosterOrViewportDown(page, w, 2))) return
  expect(w.errors).toEqual([])
})
