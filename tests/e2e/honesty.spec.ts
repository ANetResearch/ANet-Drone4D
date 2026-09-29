// Honesty labels (M16-FR-006; PRD-AC-005; AWR-03 §5.1 rule 3): the UI shows, consistent with the data:
//   illustrative coordinates ("示意坐标") for every built-in world; approximate or unknown north (shenzhen assumed,
//   suzhou unknown); synthetic ground for suzhou; "simulated" results; "参数未辨识" for the placeholder P600 profile;
//   the forced tier label when a test switch forces the backend tier.
import { expect, test } from '@playwright/test'
import { liveBackend, revealed, type Live } from './helpers'

let be: Live
test.beforeAll(async () => {
  be = await liveBackend('free-shenzhen')
})
test.afterAll(async () => {
  await be?.close()
})

const text = (page: import('@playwright/test').Page): Promise<string> => page.evaluate(() => document.body.innerText)

test('world facts are labelled honestly', async ({ page }) => {
  test.setTimeout(180_000)
  await page.goto(`${be.url}/world/shenzhen`)
  await revealed(page)
  await page.getByRole('button', { name: /世界|World/ }).first().click({ timeout: 3000 }).catch(() => {}) // the WORLD group is usually open already; without a timeout the click waited for the test timeout (INT-1)
  expect(await text(page)).toContain('示意坐标')
  expect(await text(page)).toMatch(/北向(未验证|近似)/)
  await page.goto(`${be.url}/world/suzhou`)
  await revealed(page)
  await page.getByRole('button', { name: /世界|World/ }).first().click({ timeout: 3000 }).catch(() => {}) // the WORLD group is usually open already; without a timeout the click waited for the test timeout (INT-1)
  const t = await text(page)
  expect(t).toContain('合成地面')
  expect(t).toMatch(/北向未知/)
})

test('simulation fidelity labels', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto(`${be.url}/world/shenzhen`)
  await revealed(page)
  await page.locator('[data-drone-id="p600-01"]').first().click()
  await expect(page.getByText('参数未辨识').first()).toBeVisible({ timeout: 10_000 }) // profile status arrives with R17
  const t = await text(page)
  expect(t).toContain('参数未辨识')
  expect(t).toMatch(/仿真|simulated/)
})

test('forced tier is labelled', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto(`${be.url}/world/shenzhen?tier=B`)
  await revealed(page)
  const forced = await page.evaluate(() => (window as unknown as { __perf: { forced: unknown } }).__perf.forced)
  test.skip(forced === null, 'production build ignores ?tier (forced label only exists in test builds)')
  await page.keyboard.press('KeyP')
  expect(await text(page)).toContain('测试开关强制档位')
})
