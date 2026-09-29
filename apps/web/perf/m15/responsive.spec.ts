// M15-AC-012 (functional part; M15-FR-114, AWR-14 §14): at 1280 x 720, 1600 x 900, 1920 x 1080 and 2560 x 1440 (DPR 1)
// the breakpoint defaults apply, the page never scrolls horizontally and the header, the DroneRail row and the HUD keep
// their key fields untruncated (scrollWidth <= clientWidth). UI-only test build with FakeSource (?viewport=off).
import { expect, test } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, isTestBuild, shot, startPreview } from './helpers/shell'

const PORT = 4190
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT) })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})

const SIZES = [[1280, 720, 'C'], [1600, 900, 'S'], [1920, 1080, 'S'], [2560, 1440, 'W']] as const

for (const [w, h, bp] of SIZES) {
  test(`${w} x ${h}: breakpoint ${bp}, no horizontal scroll, key fields untruncated`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h })
    await page.goto('/world/shenzhen?source=fake&fakeN=5&reveal=shell&viewport=off')
    await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 45_000 })
    test.skip(!(await isTestBuild(page)), 'needs a test build')
    await expect(page.locator('[data-rail-row]').first()).toBeVisible({ timeout: 30_000 })
    await page.locator('[data-rail-row]').first().click()
    await expect(page.locator('[data-telemetry]')).toBeVisible()
    await page.waitForTimeout(800) // page slide (08) finished
    expect(await page.evaluate(() => (window as unknown as { __ux: { layout: { breakpoint: string } } }).__ux.layout.breakpoint)).toBe(bp)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const clipped = await page.evaluate(() => {
      const out: string[] = []
      const sel = ['[data-clock]', '[data-conn]', '[data-role]', '[data-figure="perf-hud"] [data-numeric]', '[data-drone-detail] .font-mono']
      for (const s of sel) for (const el of document.querySelectorAll<HTMLElement>(s)) if (el.scrollWidth > el.clientWidth + 1) out.push(`${s}: ${el.textContent}`)
      return out
    })
    expect(clipped).toEqual([])
    await shot(page, `m15-responsive-${w}x${h}`)
  })
}
