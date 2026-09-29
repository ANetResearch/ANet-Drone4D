// Soak (M16-FR-058; D1-AC-29; PERF-AC-045; AWR-18 §8.8): soak-shenzhen (S1 + 200 x500 orbiters) for `minutes` (30);
// during it: switch the viewed world 3 times (static browsing), weather preset 5 times, overlay toggles 50 times.
// JS heap (CDP Performance.getMetrics every 5 s): median of the last 5 min over the first 5 min <= 1.2; unexpected
// reconnects 0. Backend RSS growth comes from the harness /proc sampling (server.json).
import { hotkey, palette } from './fixtures/ui'
import { caseParams, expect, pctl, test } from './fixtures/perf'

test('soak 30 min', async ({ perfPage }) => {
  const p = caseParams()
  const minutes = Number(p.minutes ?? 30)
  test.setTimeout((minutes + 5) * 60_000)
  const page = perfPage.page
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Performance.enable')
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  const heap: { t: number; v: number }[] = []
  const t0 = Date.now()
  const end = t0 + minutes * 60_000
  let k = 0
  while (Date.now() < end) {
    const m = (await cdp.send('Performance.getMetrics')) as { metrics: { name: string; value: number }[] }
    heap.push({ t: (Date.now() - t0) / 1000, v: m.metrics.find((x) => x.name === 'JSHeapUsedSize')?.value ?? Number.NaN })
    k++
    if (k % 60 === 20 && k < 200) {
      await perfPage.open('/world/shanghai')
      await perfPage.waitReveal()
      await page.waitForTimeout(3000)
      await perfPage.open('/world/shenzhen')
      await perfPage.waitReveal()
    } else if (k % 36 === 7) {
      await palette(page, '切换天气').catch(() => {})
    } else if (k % 7 === 3) {
      await hotkey(page, 'Control+KeyB')
    }
    await page.waitForTimeout(5000)
  }
  const first = heap.filter((h) => h.t <= 300).map((h) => h.v)
  const last = heap.filter((h) => h.t >= minutes * 60 - 300).map((h) => h.v)
  const growth = 100 * ((pctl(last, 0.5) ?? Number.NaN) / (pctl(first, 0.5) ?? Number.NaN) - 1)
  const snap = await perfPage.saveSnapshot()
  const reconnects = (snap.net as { reconnects: number }).reconnects
  perfPage.writeMetrics({ heap_growth_pct: growth, unexpected_reconnects: reconnects })
  expect(growth).toBeLessThanOrEqual(20)
  expect(reconnects).toBe(0)
  perfPage.assertNoPageErrors()
})
