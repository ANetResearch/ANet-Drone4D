// M12-AC-061 and M12-AC-007 (performance case, run only through perf/harness under the ADR-033 protocol; AWR-18 §3):
// Tier S (SwiftShader C1, 1280 x 720), FakeSource N = 1000 and N = 200 `scene=full`, 60 s: __perf.time.sampleP95Ms
// <= 0.3 ms (N = 1000) and <= 0.1 ms (N = 200); the clock phase p95 <= 0.02 ms; __perf.time updated every frame.
// Needs a test build (VITE_AWR_TEST_SWITCHES=1) served by the M11 static server. Tagged @perf: not part of the functional
// smoke (`npx playwright test perf/m12/smoke.spec.ts`).
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

const SECONDS = Number(process.env.M12_INTERP_S ?? 60)

for (const [n, budget] of [[1000, 0.3], [200, 0.1]] as const) {
  test(`@perf interpolation cost N = ${n} (Tier S)`, async ({ page }) => {
    const errors = watch(page)
    await page.goto(`/world/shenzhen?source=fake&fakeN=${n}&scene=full`)
    await needTestBuild(page)
    await page.waitForFunction(() => ((window as unknown as { __perf?: { time?: { frames: number } } }).__perf?.time?.frames ?? 0) > 60,
      null, { timeout: 90_000 })
    const a = await perfTime(page)
    await page.waitForTimeout(SECONDS * 1000)
    const b = await perfTime(page)
    test.info().annotations.push({ type: 'perf', description: JSON.stringify({ n, sampleP95Ms: b!.sampleP95Ms, clockP95Ms: b!.clockP95Ms, frames: b!.frames - a!.frames }) })
    expect(b!.frames - a!.frames).toBeGreaterThan(SECONDS * 10)
    expect(b!.sampleP95Ms).toBeLessThanOrEqual(budget)
    expect(b!.clockP95Ms).toBeLessThanOrEqual(0.02)
    expect(errors).toEqual([])
  })
}
