// Event storms (M16-FR-056; D1-AC-27; PERF-AC-026, 041; AWR-18 §8.7(4)):
//   RTL all      ladder n1000: select all (mod+A), return home (shift+R, confirm) -> one batched call for 1000 vehicles;
//                20 s later: our long tasks (loaf.oursOver50) 0, visible toasts <= 3, drone rail rendered rows <= visible + 10.
//   link_drop    500 vehicles (P1): link_drop through the per-vehicle R16 fault entry (no batch entry exists), then the
//                same 20 s observation and checks as RTL all (ACC-2).
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
  // ACC-2: there is no batch fault entry (the former `/api/sim/faults` call answered 404 and nothing was measured); the
  // existing per-vehicle R16 `POST /api/fleet/vehicles/{id}/faults` (operator seat) is used with the page's own principal,
  // 20 requests in flight, then the same 20 s observation and checks as 'RTL all' (D1-AC-27, AWR-18 §8.7(4)).
  test.setTimeout(300_000)
  const page = perfPage.page
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  await page.locator('[data-rail-row]').first().waitFor({ timeout: 60_000 })
  await perfPage.eval((perf) => perf.reset('frame'))
  const inj = await page.evaluate(async (count) => {
    const hint = localStorage.getItem('awr.principal_hint')
    const t = await fetch('/api/auth/token', { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ role: 'operator', client: 'web/0.1.0', ...(hint ? { principal_hint: hint } : {}) }) })
    if (!t.ok) return { error: `token HTTP ${t.status}`, ok: 0, sent: 0, ms: 0, codes: {} as Record<string, number> }
    const { token } = (await t.json()) as { token: string }
    const h = { authorization: `Bearer ${token}`, 'content-type': 'application/json' }
    const v = (await (await fetch('/api/fleet/vehicles', { headers: h })).json()) as { items?: { id: string }[] }
    const ids = (v.items ?? []).map((x) => x.id).slice(0, count)
    const codes: Record<string, number> = {}
    let ok = 0
    const t0 = performance.now()
    for (let i = 0; i < ids.length; i += 20) {
      const rs = await Promise.all(ids.slice(i, i + 20).map((id) => fetch(`/api/fleet/vehicles/${id}/faults`,
        { method: 'POST', headers: h, body: JSON.stringify({ kind: 'link_drop', params: { side: 'fcu' } }) })))
      for (const r of rs) {
        codes[r.status] = (codes[r.status] ?? 0) + 1
        if (r.status === 202) ok++
      }
    }
    return { error: null, ok, sent: ids.length, ms: performance.now() - t0, codes }
  }, 500)
  test.info().annotations.push({ type: 'linkdrop.inject', description: JSON.stringify(inj) })
  expect(inj.error, 'operator token for the page principal').toBeNull()
  expect(inj.ok, `link_drop accepted for 500 vehicles (${JSON.stringify(inj.codes)})`).toBe(500)
  await page.waitForTimeout(20_000)
  const c = await uiCounts(page)
  perfPage.writeMetrics({ toast_count: c.toasts, rail_overflow_rows: c.railRendered - c.railVisible, linkdrop_inject_ms: inj.ms })
  const snap = await perfPage.saveSnapshot()
  expect((snap.loaf as { oursOver50: number }).oursOver50).toBe(0)
  expect(c.toasts).toBeLessThanOrEqual(3)
  expect(c.railRendered - c.railVisible).toBeLessThanOrEqual(10)
  perfPage.assertNoPageErrors()
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
