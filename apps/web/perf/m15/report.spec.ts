// M15-AC-044 (report route, FR-080; AWR-18 §11.4; M16-FR-071): `/reports?src=report.json` with the JSON injected through
// page.route renders an awr.perf.report.v1 sample (schema-valid fixture) on the light theme: cover badge 480 px wide,
// every section present, data-report-ready="true" after the charts are drawn, no D1-AC-20 glyphs, at most one solid red
// per figure; `/reports/<rid>` answers "report not found" with a way back when R49 returns 404.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, glyphScan, shot, startPreview, watch } from './helpers/shell'

const PORT = 4189
const SAMPLE = readFileSync(join(import.meta.dirname, 'fixtures', 'report-sample.json'), 'utf8')
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT) })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})

test('renders a report from ?src= on the light theme and flags readiness', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/m15-report.json', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: SAMPLE }))
  await page.goto('/reports?src=/m15-report.json&viewport=off')
  const root = page.locator('[data-view="report"]')
  await expect(root).toHaveAttribute('data-report-ready', 'true', { timeout: 30_000 })
  expect(await page.evaluate(() => document.documentElement.classList.contains('dark'))).toBe(false)
  const badge = page.locator('[data-report-cover] [data-brand="badge"]')
  await expect(badge).toBeVisible()
  expect(Math.round((await badge.boundingBox())!.width)).toBe(480)
  for (const s of ['glance', 'gate', 'baseline', 'env', 'appendix']) await expect(page.locator(`[data-report-section="${s}"]`)).toBeVisible()
  await expect(page.locator('[data-figure="report-gate"] [data-hot]')).toHaveCount(1)
  for (const fig of ['report-gate', 'report-frames', 'report-cities']) {
    const reds = await page.locator(`[data-figure="${fig}"]`).evaluate((el) => {
      let n = 0
      for (const x of el.querySelectorAll('*')) {
        const cs = getComputedStyle(x)
        if (cs.backgroundColor.includes('oklch') && x.classList.contains('bg-brand-solid')) n++
      }
      return n
    })
    expect(reds, fig).toBeLessThanOrEqual(1)
  }
  expect(await glyphScan(page)).toEqual([])
  await shot(page, 'm15-report', true)
  expect(w.errors).toEqual([])
})

test('an unknown stored report shows the empty state with the way back', async ({ page }) => {
  await page.route('**/api/sys/perf-reports/**', (r) => r.fulfill({ status: 404, contentType: 'application/problem+json', body: JSON.stringify({ code: 305, reason: 305, message: 'not found' }) }))
  await page.route('**/api/auth/token', (r) => r.fulfill({ status: 404, body: '' }))
  await page.goto('/reports/p20260101-000000-abcd?viewport=off')
  await expect(page.locator('[data-view="report"]')).toHaveAttribute('data-report-ready', 'true', { timeout: 30_000 })
  await expect(page.getByRole('button', { name: '返回世界列表' })).toBeVisible()
})
