// Latency and interpolation (M16-FR-059; D1-AC-26; AWR-18 §7.3): S1 at x1, select p600-01 (60 Hz channel) and follow it;
// the drones phase records t_sim -> pixel for the focus vehicle (__perf.latency), the Worker records data age
// (__perf.net.ageMs). 30 s steady window, then snapshot; thresholds judged by the harness.
import { hotkey } from './fixtures/ui'
import { expect, test } from './fixtures/perf'

test('latency: focus vehicle t_sim to pixel', async ({ perfPage }) => {
  test.setTimeout(180_000)
  const page = perfPage.page
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  const row = page.locator('[data-drone-id="p600-01"], [data-rail-row]').first()
  await row.waitFor({ timeout: 30_000 })
  await row.click()
  await hotkey(page, 'KeyL')
  await page.waitForTimeout(2000)
  await perfPage.eval((perf) => perf.reset('latency'))
  await page.waitForTimeout(30_000)
  const snap = await perfPage.saveSnapshot()
  const lat = snap.latency as { tSimToPixelMs: number[]; holdFrames: number }
  expect(lat.tSimToPixelMs.length, 'focus vehicle latency samples').toBeGreaterThan(100)
  const net = snap.net as { selectedHz: number; swarmHz: number }
  expect(net.swarmHz).toBeGreaterThanOrEqual(9)
  perfPage.assertNoPageErrors()
})
