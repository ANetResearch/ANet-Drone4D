// Event storms (M16-FR-056; D1-AC-27; PERF-AC-026, 041; AWR-18 §8.7(4)):
//   RTL all      ladder n1000: select all (mod+A), return home (shift+R, confirm) -> one batched call for 1000 vehicles;
//                20 s later: our long tasks (loaf.oursOver50) 0, visible toasts <= 3, drone rail rendered rows <= visible + 10.
//   link_drop    500 vehicles (P1): needs a fault injection entry reachable from the UI or REST (M09 fault.inject);
//                until then the test fails with the missing interface named.
//   event flood  test build: 5000 events in 10 s through window.__uxInject.events (M15); toasts <= 3, table capped.
import { confirmDialog, hotkey, uiCounts } from './fixtures/ui'
import { expect, test } from './fixtures/perf'

test('RTL all', async ({ perfPage }) => {
  test.setTimeout(240_000)
  const page = perfPage.page
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  await page.locator('[data-rail-row]').first().waitFor({ timeout: 60_000 })
  await perfPage.eval((perf) => perf.reset('frame'))
  await hotkey(page, 'Control+KeyA')
  await hotkey(page, 'Shift+KeyR')
  await confirmDialog(page)
  await page.waitForTimeout(20_000)
  const c = await uiCounts(page)
  perfPage.writeMetrics({ toast_count: c.toasts, rail_overflow_rows: c.railRendered - c.railVisible })
  const snap = await perfPage.saveSnapshot()
  expect((snap.loaf as { oursOver50: number }).oursOver50).toBe(0)
  expect(c.toasts).toBeLessThanOrEqual(3)
  expect(c.railRendered - c.railVisible).toBeLessThanOrEqual(10)
  perfPage.assertNoPageErrors()
})

test('link_drop on 500 vehicles', async ({ perfPage }) => {
  test.setTimeout(120_000)
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  const r = await perfPage.page.request.post(`${perfPage.base}/api/sim/faults`, { data: { kind: 'link_drop', count: 500 }, failOnStatusCode: false })
  expect(r.status(), 'fault injection REST entry (M09/M11) is not available yet').toBeLessThan(300)
})

test('event flood', async ({ perfPage }) => {
  test.setTimeout(120_000)
  const page = perfPage.page
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  const ok = await page.evaluate(() => typeof (window as unknown as { __uxInject?: { events?: unknown } }).__uxInject?.events === 'function')
  expect(ok, 'test build with window.__uxInject.events (M15)').toBe(true)
  await perfPage.eval((perf) => perf.reset('frame'))
  for (let s = 0; s < 10; s++) {
    await page.evaluate((k) => {
      const inj = (window as unknown as { __uxInject: { events(e: unknown[]): void } }).__uxInject
      const batch = Array.from({ length: 500 }, (_, i) => ({ seq: k * 500 + i + 1, t_sim_ns: 0, type: 'SAF.LINK.LOST', level: 2,
        uav: `sim-${String((i % 1000) + 1).padStart(4, '0')}`, cid: null, data: {} }))
      inj.events(batch)
    }, s)
    await page.waitForTimeout(1000)
  }
  const c = await uiCounts(page)
  perfPage.writeMetrics({ toast_count: c.toasts })
  const snap = await perfPage.saveSnapshot()
  expect((snap.loaf as { oursOver50: number }).oursOver50).toBe(0)
  expect(c.toasts).toBeLessThanOrEqual(3)
  perfPage.assertNoPageErrors()
})
