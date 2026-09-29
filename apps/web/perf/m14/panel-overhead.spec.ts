// M14-AC-035 (Playwright part) / M14-NFR-015 (P1): opening the AGENTS panel (dock tab, stores/agents.ts bound to
// agent/tasks at 2 Hz and agent/*/status at 1 Hz) during flight60 `scene=full` may raise the share of frames above 50 ms by
// at most 0.5 percentage point (Tier S, local S). Panel open and closed runs alternate 3 times; statistics come from
// window.__perf.frame.interval over t in (2, 60] s (ADR-033 window). The panel is D1-ext: the test build with ?ext=1.
// This is a performance case scheduled by the M16 harness under the performance protocol (exclusive lock, load <= 4,
// AWR-18 §3) with AWR_PERF=1; it is never run in the parallel development phase (cases.mjs registers it).
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, startPreview, watch } from '../m15/helpers/shell'

const PORT = 4214
const RUNS = 3
const FLIGHT_S = 60
const WARM_S = 2
const AGENTS_TAB = '智能体'
let server: PreviewServer | null = null

interface Stats { p50: number; over50: number; n: number }

async function flight(page: Page, panel: boolean): Promise<Stats> {
  await page.goto('/world/shenzhen?bench=flight60&scene=full&city=shenzhen&source=fake&fakeN=200&ext=1')
  await page.waitForFunction(() => !document.getElementById('boot-mask'), null, { timeout: 60_000 })
  if (panel) {
    await page.getByRole('tab', { name: AGENTS_TAB }).click()
    await expect(page.locator('[data-agents-panel]')).toBeVisible({ timeout: 10_000 })
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

test.describe('@perf M14-AC-035 AGENTS panel overhead', () => {
  test.skip(process.env.AWR_PERF !== '1', 'performance case: AWR_PERF=1 under the performance protocol only')
  test.use({ baseURL: baseUrl(PORT) })
  test.beforeAll(async () => {
    server = await startPreview(PORT)
  })
  test.afterAll(async () => {
    await server?.close()
  })

  test('AGENTS panel open vs closed, alternated 3 times', async ({ page }) => {
    test.setTimeout((FLIGHT_S + 90) * 2 * RUNS * 1000)
    const w = watch(page)
    const open: Stats[] = []
    const closed: Stats[] = []
    for (let k = 0; k < RUNS; k++) {
      closed.push(await flight(page, false))
      open.push(await flight(page, true))
    }
    const med = (a: number[]) => [...a].sort((x, y) => x - y)[Math.floor(a.length / 2)]
    const overOpen = med(open.map((s) => s.over50))
    const overClosed = med(closed.map((s) => s.over50))
    test.info().annotations.push({ type: 'm14-ac-035', description: JSON.stringify({ overOpen, overClosed, open, closed }) })
    expect(w.errors).toEqual([])
    expect(overOpen - overClosed).toBeLessThanOrEqual(0.005)
  })
})
