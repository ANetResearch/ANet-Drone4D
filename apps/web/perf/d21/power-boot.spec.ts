// Power save must not start before the reveal (WP-13b, M16; D2-AC-44, AWR-04 §15.7.3; found while writing gpu-limits-d21).
// Before D2-INT the PowerController's idle clock ran from the page load: with an idle threshold shorter than the boot (test switches
// `?power=viewer&powerIdle=12`, FakeSource on the M06 test server, about 17-22 s to the reveal) the page went ACTIVE > QUIET >
// SAVING behind the boot mask, the frame loop was suspended and the mask never lifted ("加载首屏点云 · 0.0 / 0.5 MB" for minutes).
// With the real thresholds (300 s viewer, 60 s hidden) the same happened to a page loaded in a background tab or on a slow
// device. Expected: no idle timing (and no saving) while the boot mask is up (d2-WP-13b-to-M15 item 3, fixed in D2-INT:
// app/power/PowerController.ts starts the idle clock at the reveal).
import { expect, test } from '@playwright/test'
import { startM06Server, type M06Server } from '../m06/server'
import { ensureBuild, isTestBuild } from './live21'

let srv: M06Server | null = null
test.beforeAll(async () => {
  test.setTimeout(600_000)
  srv = await startM06Server(undefined, ensureBuild('test'))
})
test.afterAll(async () => {
  await srv?.close()
})

test('a short idle threshold does not hold the boot mask', async ({ page }) => {
  test.setTimeout(180_000)
  await page.goto(`${srv!.url}/world/synthcity?source=fake&fakeWorld=synthcity&fakeN=6&fakeGround=1&power=viewer&powerIdle=12&powerWarn=3&powerQuiet=5`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 90_000 })
  expect(await isTestBuild(page)).toBe(true)
  const tr = await page.evaluate(() => (window as unknown as { __ux: { power: { transitions: string[] } } }).__ux.power.transitions)
  expect(tr.filter((x) => x.includes('SAVING'))).toEqual([])
})
