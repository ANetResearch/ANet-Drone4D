// M15 screenshot self-check of the D2 pages (WP-12b; AWR-14 §20.5 to §20.13; M15-AC-218 layout part): the sandbox surface on
// the real viewport (worlds served from the repository, FakeSource replay of fixtures/d2/sandbox_t5.awrrt, the scripted REST
// of helpers/d2rest.ts) at 1920 x 1080 and 1280 x 720: main view, targets list, detail and advanced properties, nests,
// model catalogue and clone form, the new task page drawn with real clicks on the map (ray hit) and its preview on the
// planning layer, the task and relay tabs, FPV with the sensor views and the HUD, the manual control confirmation, the GSD
// calculator, the base map and template dialogs. Every page: no page error, no forbidden glyph. The images go to
// .cache/impl/shots (AWR_SHOTS_DIR overrides) and are reviewed by eye (WP-12b report §3).
// Build: `M15_DIST=<dir> node perf/m15/helpers/build-ui.mjs`; run: `M15_DIST=<dir> npx playwright test perf/m15/shots-d2.spec.ts
// --project perf` (needs worlds/ and the real viewport: skipped with M15_UI_ONLY=1).
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { dockTab, open, routes, SIZES, storeSession } from './helpers/d2rest'
import { baseUrl, glyphScan, shot, startPreview, watch } from './helpers/shell'
import { serveWorlds } from './helpers/worlds'

const PORT = 4197
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT), viewport: { width: 1920, height: 1080 } })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})
test.describe.configure({ mode: 'serial' })

/** both sizes; `reopen` restores what the breakpoint layout folded (the 1280 x 720 preset closes the Dock) */
async function both(page: Page, name: string, settle = 300, reopen?: () => Promise<void>): Promise<void> {
  for (const s of SIZES) {
    await page.setViewportSize({ width: s.w, height: s.h })
    await page.waitForTimeout(reopen ? Math.max(settle, 1200) : settle)
    if (reopen) await reopen()
    await shot(page, `m15-d2s-${name}-${s.w}x${s.h}`)
  }
  await page.setViewportSize({ width: 1920, height: 1080 })
}

test('D2 pages on the sandbox surface', async ({ page, context }) => {
  test.skip(process.env.M15_UI_ONLY === '1', 'needs the real viewport (M06)')
  const w = watch(page)
  await routes(context, { posts: [], preview: 'ok' })
  test.skip(!(await serveWorlds(context)), 'worlds/ missing (make worlds)')
  await storeSession(page)
  await open(page, true)
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  await page.waitForTimeout(1500)
  await both(page, 'main')

  // targets: list, detail, advanced properties
  await page.locator('[data-entity="targets"]').click()
  await expect(page.locator('[data-target-row]').first()).toBeVisible({ timeout: 20_000 })
  await both(page, 'targets')
  await page.locator('[data-target-row]').first().click()
  await expect(page.locator('[data-target-detail]')).toBeVisible()
  await both(page, 'target-detail')
  await page.locator('[data-target-advanced-open]').click()
  await expect(page.locator('[data-target-advanced]')).toBeVisible()
  await both(page, 'target-advanced')
  await page.keyboard.press('Escape')
  await page.locator('[data-target-back]').click()

  // nests
  await page.locator('[data-entity="nests"]').click()
  await expect(page.locator('[data-nest-row]').first()).toBeVisible({ timeout: 20_000 })
  await page.locator('[data-nest-row]').first().click()
  await both(page, 'nest-detail')
  await page.locator('[data-nest-detail] button').first().click()

  // fleet: catalogue and clone form
  await page.locator('[data-entity="drones"]').click()
  await page.locator('[data-fleet-catalog]').click()
  await expect(page.locator('[data-model-card]').first()).toBeVisible()
  await page.locator('[data-model-detail="p600_mid360"]').click()
  await both(page, 'catalog')
  await page.locator('[data-model-clone="p600_mid360"]').click()
  await both(page, 'clone')
  await page.keyboard.press('Escape')

  // new task drawn on the map (ray hit), preview on the planning layer
  await dockTab(page, /^任务$/)
  await page.locator('[data-task-new-open]').click()
  await page.locator('[data-task-type="area_guard"]').click()
  const canvas = page.locator('[data-viewport] canvas')
  const box = await canvas.boundingBox()
  if (box) {
    const cx = box.x + box.width * 0.42
    const cy = box.y + box.height * 0.5
    for (const [dx, dy] of [[-90, -50], [90, -60], [110, 60], [-70, 70]]) {
      await page.mouse.click(cx + dx, cy + dy)
      await page.waitForTimeout(350)
    }
    await both(page, 'task-drawing')
    await page.keyboard.press('Enter')
  }
  const params = page.locator('[data-task-params="area_guard"]')
  if (await params.isVisible({ timeout: 5000 }).catch(() => false)) {
    await page.locator('[data-task-preview]').click()
    await expect(page.locator('[data-task-preview-card="done"]')).toBeVisible({ timeout: 10_000 })
    await both(page, 'task-preview', 600)
  }
  await page.locator('[data-task-back]').click()

  // task and relay tabs
  await dockTab(page, /^任务$/)
  await expect(page.locator('[data-task-row="task-01"]')).toBeVisible({ timeout: 20_000 })
  await both(page, 'tasks', 300, () => dockTab(page, /^任务$/))
  await dockTab(page, /^接力监控$/)
  await expect(page.locator('[data-relay-panel="task-02"]')).toBeVisible()
  await both(page, 'relay', 300, () => dockTab(page, /^接力监控$/))
  await page.keyboard.press('Backquote')

  // FPV, sensor views and HUD
  await page.locator('[data-rail-row]').filter({ hasText: 'uav0001' }).first().click()
  await page.keyboard.press('Digit4')
  await expect(page.locator('[data-sensor-view-bar]')).toBeVisible({ timeout: 20_000 })
  await both(page, 'fpv', 600)
  for (const v of ['eo', 'nir', 'lwir'] as const) {
    const b = page.locator(`[data-sensor-view="${v}"]`)
    if (await b.isEnabled()) {
      await b.click()
      await page.waitForTimeout(600)
      await shot(page, `m15-d2s-fpv-${v}-1920x1080`)
    }
  }
  await page.locator('[data-sensor-view="fpv"]').click()

  // manual control confirmation
  const takeover = page.locator('[data-manual-takeover]')
  if (await takeover.count()) {
    await takeover.first().click()
    await page.waitForTimeout(400)
    await shot(page, 'm15-d2s-manual-confirm-1920x1080')
    await page.keyboard.press('Escape')
  }
  await page.keyboard.press('Digit1')

  // GSD calculator, base map and template dialogs
  await page.keyboard.press('Control+k')
  await page.keyboard.type('GSD')
  await page.keyboard.press('Enter')
  await expect(page.locator('[data-gsd-calc]')).toBeVisible()
  await both(page, 'gsd')
  await page.keyboard.press('Escape')
  await page.locator('[data-world-switch]').click()
  await page.waitForTimeout(400)
  await both(page, 'world-switch')
  await page.keyboard.press('Escape')
  await page.locator('[data-world-template]').click()
  await page.waitForTimeout(400)
  await both(page, 'template')
  await page.keyboard.press('Escape')
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors).toEqual([])
})
