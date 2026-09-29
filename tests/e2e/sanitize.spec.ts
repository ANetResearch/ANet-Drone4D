// Runtime sanitisation (D1-AC-20 item 5; M16-FR-012): scenario names and agent or event texts containing emoji and
// forbidden glyphs are stripped before display (lib/sanitize.ts); arrows U+2190-U+2193 and math symbols are kept.
import { expect, test } from '@playwright/test'
import { glyphScan, isTestBuild, revealed } from './helpers'

const DIRTY = 'S1 \u{1F680} facade \u{2605} ok \u{2192} \u{B1}'

test('scenario names from the api are sanitised', async ({ page }) => {
  test.setTimeout(120_000)
  await page.route('**/api/scenarios**', (route) => route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ items: [{ id: 's1-shenzhen-facade', world_id: 'shenzhen', name: DIRTY, name_zh: DIRTY, rate: 1,
      gcs_loss_policy: 'ignore', n_vehicles: 2, time_limit_s: 1800, sha256: '0'.repeat(64), tags: [], profiles: [] }] }) }))
  await page.goto('/world/shenzhen?source=fake&fakeN=2')
  await revealed(page)
  test.skip(!(await isTestBuild(page)), 'needs a test build (window.__uxInject); a skip is a failure in the harness')
  await page.evaluate((txt) => {
    const w = window as unknown as { __uxInject: { events(e: unknown[]): void; flush(): void } }
    w.__uxInject.events([{ seq: 1, t_sim_ns: 0, type: 'scenario.mark', level: 2, uav: null, cid: null, data: { label: txt } }])
    w.__uxInject.flush()
  }, DIRTY)
  await page.waitForTimeout(1500)
  expect(await glyphScan(page)).toEqual([])
})
