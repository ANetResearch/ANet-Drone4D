// D1-AC-23 / M15-AC-058 / M15-NFR-001 (UI overhead, P1): flight60 `scene=full` on the production build, "UI shell + HUD +
// DroneRail open" against "canvas only" (?chrome=0, AWR-18 §9.5), alternated 3 times each: the presented-interval p50
// must not change and the share of frames above 50 ms may grow by at most 1 percentage point. Statistics come from
// window.__perf.frame.interval over t in (2, 60] s (ADR-033 window); no __ux (production build, AWR-18 PR-7).
// This is a performance case: it runs only under the performance protocol (exclusive lock, load <= 4; AWR-18 §3) with
// AWR_PERF=1 and a running backend or FakeSource (`?source=fake&fakeN=200`); the gate copy is apps/web/perf/ui-overhead
// .spec.ts of M16 (AWR-18 §8.5). Never run in the parallel development phase.
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, startPreview } from './helpers/shell'

const PORT = 4188
const RUNS = 3
const FLIGHT_S = 60
const WARM_S = 2
let server: PreviewServer | null = null

test.describe('@perf D1-AC-23 UI overhead', () => {
  test.skip(process.env.AWR_PERF !== '1', 'performance case: AWR_PERF=1 under the performance protocol only')
  test.use({ baseURL: baseUrl(PORT) })
  test.beforeAll(async () => {
    server = await startPreview(PORT)
  })
  test.afterAll(async () => {
    await server?.close()
  })

  interface Stats { p50: number; over50: number; n: number }
  async function flight(page: Page, chrome: boolean): Promise<Stats> {
    const q = `bench=flight60&scene=full&city=shenzhen&source=fake&fakeN=200${chrome ? '' : '&chrome=0'}`
    await page.goto(`/world/shenzhen?${q}`)
    await page.waitForFunction(() => !document.getElementById('boot-mask'), null, { timeout: 60_000 })
    if (chrome) {
      // DroneRail open and the HUD expanded (the layout defaults at 1280 x 720 keep both)
      await expect(page.locator('[data-rail-row]').first()).toBeVisible({ timeout: 30_000 })
      await expect(page.locator('[data-figure="perf-hud"]')).toBeVisible()
    }
    await page.evaluate(() => (window as unknown as { __perf: { reset(): void } }).__perf.reset())
    await page.waitForTimeout(FLIGHT_S * 1000)
    return page.evaluate(([warm]) => {
      const p = (window as unknown as { __perf: { frame: { interval: { buf: Float64Array; n: number }; t: { buf: Float64Array; n: number } } } }).__perf
      const r = p.frame.interval
      const tr = p.frame.t
      const m = Math.min(r.n, r.buf.length)
      const mask = r.buf.length - 1
      const t0 = tr.buf[(tr.n - m) & mask]
      const xs: number[] = []
      for (let i = 0; i < m; i++) {
        const t = tr.buf[(tr.n - m + i) & mask]
        if (t - t0 > warm * 1000) xs.push(r.buf[(r.n - m + i) & mask])
      }
      xs.sort((a, b) => a - b)
      const over = xs.filter((x) => x > 50).length
      return { p50: xs[Math.floor(xs.length / 2)] ?? Number.NaN, over50: xs.length ? over / xs.length : Number.NaN, n: xs.length }
    }, [WARM_S] as const)
  }

  test('UI shell + HUD + DroneRail vs canvas only, alternated 3 times', async ({ page }) => {
    test.setTimeout((FLIGHT_S + 90) * 2 * RUNS * 1000)
    const ui: Stats[] = []
    const bare: Stats[] = []
    for (let k = 0; k < RUNS; k++) {
      bare.push(await flight(page, false))
      ui.push(await flight(page, true))
    }
    const med = (a: number[]) => [...a].sort((x, y) => x - y)[Math.floor(a.length / 2)]
    const p50Ui = med(ui.map((s) => s.p50))
    const p50Bare = med(bare.map((s) => s.p50))
    const overUi = med(ui.map((s) => s.over50))
    const overBare = med(bare.map((s) => s.over50))
    test.info().annotations.push({ type: 'd1-ac-23', description: JSON.stringify({ p50Ui, p50Bare, overUi, overBare, ui, bare }) })
    // "p50 unchanged": the same presentation bucket (one refresh interval of the target, T* = 33.3 ms on Tier S)
    expect(Math.abs(p50Ui - p50Bare)).toBeLessThan(16.7)
    expect(overUi - overBare).toBeLessThanOrEqual(0.01)
  })
})
