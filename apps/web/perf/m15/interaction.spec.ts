// M15 UI interaction specs on a test build with FakeSource (M15 §10 NFR-018: UI single cases driven by FakeSource; no
// backend). They cover the M15 part of D1-AC-32 and D1-AC-27 and M15-AC-017, AC-020 (UI side), AC-026 (presentation),
// AC-028, AC-047, AC-052 and AC-057 (P0 part): the DroneRail rows and the detail page command group with its state
// machine, the client-side pre-check, the read-only viewer notice, the offline banner, toast merging under an event
// storm (<= 3 visible), the alarm centre with the one red of the header, the 1000-vehicle rail (rendered rows <= visible
// + 10) with a batch return home (one fleet call, one aggregate toast), the command palette and the shortcut help.
// `?viewport=off&reveal=shell` (test builds, ui/testing/uiOnly.ts) runs the shell without the WorldCanvas and without
// the point cloud and shader warm-up gates, so the UI can be exercised while those modules change in parallel. Build: VITE_AWR_TEST_SWITCHES=1 npx vite build [--outDir $M15_DIST].
// Screenshots: .cache/impl/shots/m15-*.png.
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import {
  baseUrl, glyphScan, backdropFilters, injectConn, injectEvents, isTestBuild, shot, startPreview, unnamedButtons, visibleToasts, watch,
  type InjectEvent,
} from './helpers/shell'

const PORT = 4186
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT) })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})
test.describe.configure({ mode: 'serial' })

async function open(page: Page, n: number, extra = ''): Promise<void> {
  await page.goto(`/world/shenzhen?source=fake&fakeN=${n}&reveal=shell&viewport=off${extra}`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 45_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
  await expect(page.locator('[data-rail-row]').first()).toBeVisible({ timeout: 30_000 })
}

let seq = 1_000_000
const event = (type: string, level: 0 | 1 | 2 | 3, uav: string | null, data: Record<string, unknown> = {}): InjectEvent =>
  ({ seq: ++seq, t_sim_ns: seq * 1e6, type, level, uav, cid: null, data })

test('DroneRail row, detail page and the command state machine (FakeSource N = 1)', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1600, height: 900 })
  await open(page, 1)
  await expect(page.locator('[data-rail-row]')).toHaveCount(1)
  await expect(page.locator('[data-role="operator"]')).toBeVisible()
  await page.locator('[data-drone-id="p600-01"]').click()
  const detail = page.locator('[data-drone-detail="p600-01"]')
  await expect(detail).toBeVisible()
  // the fake vehicle flies: take-off is refused by the pre-check (reason in the Tooltip), hover runs
  const takeoff = page.locator('[data-cmd="takeoff"]')
  await expect(takeoff).toHaveAttribute('aria-disabled', 'true')
  await takeoff.hover()
  await expect(page.locator('[data-slot="tooltip-content"]')).toContainText('机体已在空中')
  const hover = page.locator('[data-cmd="hover"]')
  await expect(hover).toBeEnabled()
  await hover.click()
  await expect(hover).toHaveAttribute('data-cmd-state', 'succeeded', { timeout: 10_000 })
  await expect(page.locator('[data-telemetry]')).toBeVisible()
  await page.waitForTimeout(500)
  await shot(page, 'm15-drone-detail')
  // land asks first; the initial focus is Cancel (UX-FR-050)
  await page.locator('[data-cmd="land"]').click()
  const dialog = page.locator('[data-slot="alert-dialog-content"]')
  await expect(dialog).toBeVisible()
  await expect(page.locator('[data-slot="alert-dialog-cancel"]')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0, { timeout: 15_000 })
  expect(await glyphScan(page)).toEqual([])
  expect(await backdropFilters(page)).toBe(0)
  expect(w.errors.filter((e) => !/loop task/.test(e))).toEqual([])
})

test('viewer: one read-only notice, refused hotkeys flash the reason; offline banner after the delay', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await open(page, 1)
  await page.locator('[data-drone-id="p600-01"]').click()
  await injectConn(page, { role: 'viewer', seat: 'none' })
  await expect(page.locator('[data-readonly-commands]')).toBeVisible()
  await expect(page.locator('[data-cmd="hover"]')).toHaveCount(0)
  await expect(page.locator('[data-role="viewer"]')).toContainText('只读')
  await page.locator('[data-figure="header"]').click({ position: { x: 5, y: 5 } })
  await page.keyboard.press('KeyH')
  await expect(page.locator('[data-tool-hint]')).toContainText('只读模式')
  await shot(page, 'm15-viewer-readonly')
  await injectConn(page, { role: 'operator', seat: 'held', conn: 'RECONNECTING', attempt: 3, nextInMs: 2000, downSinceMs: Date.now() - 12_000, bannerVisible: true })
  await expect(page.locator('[data-conn-banner="RECONNECTING"]')).toBeVisible()
  await expect(page.locator('[data-conn="RECONNECTING"]')).toBeVisible()
  await expect(page.locator('[data-badge="offline"]')).toBeVisible()
  await page.waitForTimeout(400)
  await shot(page, 'm15-offline-banner')
  await injectConn(page, { conn: 'LIVE', bannerVisible: false, downSinceMs: Number.NaN })
  await expect(page.locator('[data-conn-banner]')).toHaveCount(0)
})

test('event storm: toasts merge to <= 3, one alarm per key, one red in the header (D1-AC-27, M15-AC-047)', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await open(page, 1)
  const batch: InjectEvent[] = []
  for (let i = 0; i < 500; i++) batch.push(event('safety.link', 2, `uav${String(i).padStart(4, '0')}`, { reason: 'link_drop' }))
  for (let i = 0; i < 6; i++) batch.push(event('env.warning', 2, null, { reason: `ENV_${i}`, message: `wind ${i}` }))
  batch.push(event('uav.state', 3, 'p600-05', { to: 'FAILSAFE/CONTROLLED' }))
  batch.push(event('uav.state', 3, 'p600-06', { to: 'CRASHED/IMPACT' }))
  await injectEvents(page, batch)
  await page.waitForTimeout(800)
  expect(await visibleToasts(page)).toBeLessThanOrEqual(3)
  const alarm = page.locator('[data-alarm-button]')
  await expect(page.locator('[data-alarm-count]')).toHaveAttribute('data-alarm-count', '2')
  await page.waitForTimeout(600) // one-red evaluation (250 ms)
  expect(await page.locator('[data-figure="header"] .bg-brand-solid').count()).toBeLessThanOrEqual(1)
  await shot(page, 'm15-toast-storm')
  await alarm.click()
  const center = page.locator('[data-alarm-center]')
  await expect(center).toBeVisible()
  await expect(center.locator('[data-alarm]')).toHaveCount(9)
  await page.waitForTimeout(400)
  await shot(page, 'm15-alarm-center')
  await center.getByRole('button', { name: '全部确认' }).click()
  // acknowledged: the count badge turns to an outline or disappears (M15-AC-047)
  await expect(page.locator('[data-figure="header"] .bg-brand-solid')).toHaveCount(0)
  await page.keyboard.press('Escape')
  // the Events tab lists the storm (virtualised) and a row click selects the vehicle
  await page.keyboard.press('Backquote')
  await page.getByRole('tab', { name: '事件' }).click()
  await expect(page.locator('[data-events-panel]')).toBeVisible()
  await page.waitForTimeout(400)
  await shot(page, 'm15-events')
  const rows = await page.locator('[data-lf-table] [data-row-key]').count()
  expect(rows).toBeGreaterThan(0)
  expect(rows).toBeLessThan(80)
})

test('1000 vehicles: rendered rail rows <= visible + 10; batch return home is one call (D1-AC-27, M15-AC-017, AC-029)', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await open(page, 1000)
  await expect.poll(() => page.evaluate(() => (window as unknown as { __ux: { droneRail: { renderedRows: number } } }).__ux.droneRail.renderedRows), { timeout: 20_000 }).toBeGreaterThan(5)
  const count = async () => page.locator('[data-rail-row]').count()
  const visible = async () => page.evaluate(() => (window as unknown as { __ux: { droneRail: { visibleRows: number } } }).__ux.droneRail.visibleRows)
  expect(await count()).toBeLessThanOrEqual((await visible()) + 10)
  await page.locator('[data-figure="drone-rail"]').evaluate((el) => el.scrollTo({ top: el.scrollHeight }))
  await page.waitForTimeout(400)
  expect(await count()).toBeLessThanOrEqual((await visible()) + 10)
  await shot(page, 'm15-rail-1000')
  // Mod+A selects the filtered fleet; the batch bar sends one fleet/cmd/rtl after confirmation
  await page.locator('[data-figure="header"]').click({ position: { x: 5, y: 5 } })
  await page.keyboard.press('Control+KeyA')
  await expect(page.locator('[data-batch-bar]')).toBeVisible()
  await page.locator('[data-batch-bar] [data-cmd="rtl"]').click()
  await expect(page.locator('[data-cmd-confirm="rtl"]')).toBeVisible()
  await expect(page.locator('[data-cmd-confirm="rtl"]')).toContainText('1000')
  await page.locator('[data-cmd-confirm="rtl"]').click()
  // the aggregate call is accepted (children fly home; the summary turns final when all children end)
  await expect(page.locator('[data-batch-bar] [data-cmd="rtl"]')).toHaveAttribute('data-cmd-state', /accepted|running|succeeded/, { timeout: 15_000 })
  await expect(page.locator('[data-slot="toast"]').filter({ hasText: '1000' })).toHaveCount(1, { timeout: 10_000 })
  await page.waitForTimeout(600)
  expect(await visibleToasts(page)).toBeLessThanOrEqual(3)
  await shot(page, 'm15-batch-rtl')
})

test('command palette finds a vehicle; shortcut help lists the table; icon buttons are named (D1-AC-21 P0)', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await open(page, 1)
  expect(await unnamedButtons(page)).toEqual([])
  // editable fields keep their keys: a backquote typed into the rail search does not open the Dock
  const dockBefore = await page.locator('[data-slot="rail-host"][data-side="bottom"]').getAttribute('data-state')
  await page.getByRole('textbox', { name: '搜索机体' }).fill('`')
  expect(await page.locator('[data-slot="rail-host"][data-side="bottom"]').getAttribute('data-state')).toBe(dockBefore)
  await page.getByRole('textbox', { name: '搜索机体' }).fill('')
  await page.keyboard.press('Control+KeyK')
  await expect(page.locator('[data-slot="dialog-content"]')).toBeVisible()
  await page.getByRole('combobox').fill('p600')
  await expect(page.locator('[data-palette-vehicle="p600-01"]')).toBeVisible()
  await page.waitForTimeout(300)
  await shot(page, 'm15-palette-vehicle')
  await page.locator('[data-palette-vehicle="p600-01"]').click()
  await expect.poll(() => page.evaluate(() => (window as unknown as { __ux: { selection: { primary: string | null } } }).__ux.selection.primary)).toBe('p600-01')
  await expect(page.locator('[data-slot="dialog-content"]')).toHaveCount(0, { timeout: 15_000 })
  // Esc chain: clears the selection when nothing else is open
  await page.locator('[data-figure="header"]').click({ position: { x: 5, y: 5 } })
  await page.keyboard.press('Escape')
  await expect.poll(() => page.evaluate(() => (window as unknown as { __ux: { selection: { primary: string | null } } }).__ux.selection.primary)).toBeNull()
  await page.keyboard.press('Shift+Slash')
  await expect(page.locator('[data-shortcut-table]')).toBeVisible()
  expect(await page.locator('[data-shortcut-table] [data-hotkey]').count()).toBeGreaterThanOrEqual(34)
  await page.waitForTimeout(300)
  await shot(page, 'm15-shortcuts-full')
})

test('panels at 1920 x 1080: world, layers, environment, perf, timeline and mission', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1920, height: 1080 })
  await open(page, 3)
  await expect(page.locator('[data-env-panel]')).toBeVisible()
  await expect(page.locator('[data-env-panel] [data-preset]')).toHaveCount(12)
  await expect(page.locator('[data-world-panel]')).toBeVisible()
  await page.waitForTimeout(500)
  await shot(page, 'm15-left-rail')
  await page.keyboard.press('Backquote')
  await page.getByRole('tab', { name: '性能' }).click()
  await expect(page.locator('[data-perf-panel] [data-lf-card]')).toHaveCount(11)
  await page.waitForTimeout(1500)
  await shot(page, 'm15-perf-panel')
  await page.getByRole('tab', { name: '任务' }).click()
  await expect(page.locator('[data-mission-panel]')).toBeVisible()
  await page.getByRole('tab', { name: '图表' }).click()
  await page.locator('[data-drone-id]').first().click()
  await page.waitForTimeout(1500)
  await shot(page, 'm15-charts')
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors.filter((e) => !/loop task/.test(e))).toEqual([])
})
