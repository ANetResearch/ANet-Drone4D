// Automatic rehearsal of the demo script (M16-FR-028; M16-AC-018; M16 §6.5; 13 §4.4). Headless D0-D5 with the checks of
// the M16 §6.5 table; AWR_DEMO_RECORD=1 (make demo-rehearse RECORD=1) records a 1280 x 720 webm as the fallback video
// (runs/demo/<date>/mainline.webm). Backend: S1 with the demo profile (on_complete continue) from the harness, or a
// supervisor started here. Performance thresholds (TTFP, switch) are only asserted under the harness (AWR_PERF=1).
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import { glyphScan, liveBackend, revealed, watch, type Live } from './helpers'

const ROOT = join(import.meta.dirname, '..', '..')
const PERF = process.env.AWR_PERF === '1'
const RECORD = process.env.AWR_DEMO_RECORD === '1'
let be: Live
test.beforeAll(async () => {
  be = await liveBackend('s1-shenzhen-facade')
})
test.afterAll(async () => {
  await be?.close()
})

type P = { __perf: { load: { ttfp: number; switchMs: number }; gpu: { programs: number } } }

test('demo D0-D5', async ({ browser }) => {
  test.setTimeout(900_000)
  const videoDir = join(ROOT, 'runs', 'demo', new Date().toISOString().slice(0, 10).replaceAll('-', ''))
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 },
    ...(RECORD ? { recordVideo: { dir: videoDir, size: { width: 1280, height: 720 } } } : {}) })
  const page = await ctx.newPage()
  const w = watch(page)
  // D0 opening
  await page.goto(`${be.url}/world/shenzhen`)
  await revealed(page)
  const ttfp = await page.evaluate(() => (window as unknown as P).__perf.load.ttfp)
  if (PERF) expect(ttfp).toBeLessThanOrEqual(1000)
  // D1 world: colour modes do not compile; switch to shanghai and back
  const prog0 = await page.evaluate(() => (window as unknown as P).__perf.gpu.programs)
  for (const mode of ['HAG', 'Class', 'Height']) {
    await page.keyboard.press('Control+KeyK')
    await page.getByPlaceholder('输入命令或搜索').fill(mode)
    await page.keyboard.press('Enter')
    await page.waitForTimeout(800)
  }
  expect(await page.evaluate(() => (window as unknown as P).__perf.gpu.programs)).toBe(prog0)
  await page.goto(`${be.url}/world/shanghai`)
  await revealed(page)
  const sw = await page.evaluate(() => (window as unknown as P).__perf.load.switchMs)
  if (PERF) expect(sw).toBeLessThanOrEqual(1500)
  await page.goto(`${be.url}/world/shenzhen`)
  await revealed(page)
  // D2 fleet: both S1 vehicles in the drone rail, follow, trails and frustums
  await expect(page.locator('[data-rail-row]')).toHaveCount(2, { timeout: 30_000 })
  await page.locator('[data-rail-row]').first().click()
  for (const k of ['KeyL', 'KeyT', 'KeyV']) {
    await page.keyboard.press(k)
    await page.waitForTimeout(500)
  }
  await page.waitForTimeout(10_000)
  // D3 environment: weather presets through the palette
  for (const name of ['雨', '雷']) {
    await page.keyboard.press('Control+KeyK')
    await page.getByPlaceholder('输入命令或搜索').fill(`切换天气 ${name}`)
    await page.keyboard.press('Enter')
    await page.waitForTimeout(5000)
  }
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors, w.errors.join('\n')).toEqual([])
  const video = page.video()
  await ctx.close()
  if (RECORD && video) {
    mkdirSync(videoDir, { recursive: true })
    renameSync(await video.path(), join(videoDir, 'mainline.webm'))
  }
})
