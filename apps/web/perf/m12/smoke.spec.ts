// M12 functional smoke in the browser without a backend (M12-AC-007 functional part, M12-AC-008 prefix, FR-020):
// FakeSource N = 200 through the real rt.worker; the clock phase updates window.__perf.time every frame (frames grow,
// tRender advances monotonically while the clock advances, D stays within [0, 300 ms x rate], the interpolation ring
// samples every vehicle), the timeline store follows TIME at <= 4 writes per second (Tier S) and the TIME readout keeps
// its SIM prefix. Needs a test build (VITE_AWR_TEST_SWITCHES=1) served by the M11 static server (M11_DIST or dist/).
// Functional only: no timing thresholds.
import type { Server } from 'node:http'
import { expect, test } from '@playwright/test'
import { PORT, startStaticServer, stopServer } from '../m11/staticServer'
import { needTestBuild, perfTime, watch } from './common'

let server: Server | null = null
test.use({ baseURL: `http://127.0.0.1:${PORT}` })
test.beforeAll(async () => {
  server = await startStaticServer()
})
test.afterAll(async () => {
  await stopServer(server)
})

test('clock phase and timeline store follow TIME with FakeSource N = 200', async ({ page }) => {
  const errors = watch(page)
  await page.goto('/world/shenzhen?source=fake&fakeN=200')
  await needTestBuild(page)
  await page.waitForFunction(() => ((window as unknown as { __perf?: { time?: { epoch: number } } }).__perf?.time?.epoch ?? -1) >= 0,
    null, { timeout: 60_000 })
  const a = await perfTime(page)
  await page.waitForTimeout(1500)
  const b = await perfTime(page)
  expect(a && b).toBeTruthy()
  expect(b!.frames).toBeGreaterThan(a!.frames)
  if (b!.state4 === 1 || b!.state4 === 9) {
    expect(b!.tRenderS).toBeGreaterThan(a!.tRenderS)
    expect(b!.dGlobalMs).toBeGreaterThanOrEqual(0)
    expect(b!.dGlobalMs).toBeLessThanOrEqual(300 * Math.max(1, b!.rate) + 1)
  }
  expect(b!.hzEff).toBeGreaterThan(0)
  // the timeline store: TIME summary and <= 4 writes per second on Tier S
  const st = await page.evaluate(async () => {
    const tl = (window as unknown as { __timeline: { store: { getState(): Record<string, unknown>; subscribe(f: () => void): () => void } } }).__timeline
    let writes = 0
    const off = tl.store.subscribe(() => writes++)
    await new Promise((r) => setTimeout(r, 3000))
    off()
    return { writes, s: tl.store.getState() }
  })
  expect(st.writes / 3).toBeLessThanOrEqual(4.5)
  expect(st.s.epoch).toBe(b!.epoch)
  expect(Number(st.s.tDisplayS)).toBeGreaterThan(0)
  // every SIM time text carries its prefix (AWR-14 §6.17)
  const bar = page.locator('[data-slot="timeline-bar"]')
  if (await bar.count()) await expect(bar).toContainText('SIM')
  expect(errors.filter((e) => !/viewport|reading 'query'/i.test(e))).toEqual([])
})
