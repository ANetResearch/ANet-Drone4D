// M15 D2 functional specs of WP-12b (M15-AC-207, AC-209, AC-210, AC-212, AC-214 functional parts; UX-FR-126 to UX-FR-131,
// UX-FR-136, UX-FR-137, UX-FR-144) on a test build: the sandbox REST scripted by page routes (contracts fixtures plus the
// task, plan, preview and create replies below), the realtime link on FakeSource replaying `fixtures/d2/sandbox_t5.awrrt`
// (task/task-01 area guard, task/task-02 relay watch, relay/task-02/stats, nest/nest-01/status, uav0001 perception and
// payload). No backend; functional only (no timing thresholds).
//   * Dock: the task tab replaces the D1 mission tab, rows of both tasks, the selected row's work items; the relay tab:
//     KPI, stations with the vehicle on station, next relief countdown, gaps by cause;
//   * new task page: type, (injected) polygon, pre-check, preview allocation (card: vehicles, why n, curve with T_max,
//     assignments, sustainability), start with the preview id; an infeasible preview (422 580 fleet_short) shows the reason
//     and "add vehicle";
//   * nests from the tasking store (status topic), the catalogue Sheet from the catalogue store, the GSD calculator;
//   * the real viewport (worlds served from the repository): FPV camera on uav0001, the sensor view bar (views follow the
//     vehicle's sensor poses), the HUD pixel progress of the perception frame and the capture boxes; screenshots of every page at
//     1920 x 1080 and 1280 x 720 (.cache/impl/shots, AWR_SHOTS_DIR overrides).
// Build: `M15_DIST=<dir> node perf/m15/helpers/build-ui.mjs`; run: `M15_DIST=<dir> npx playwright test perf/m15/tasks-d2.spec.ts
// --project perf` (AWR_PORT_OFFSET for a private port).
import { expect, test } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { dockTab, open, routes, SID, SIZES, storeSession, type Script } from './helpers/d2rest'
import { baseUrl, glyphScan, injectConn, isTestBuild, shot, startPreview, watch } from './helpers/shell'
import { serveWorlds } from './helpers/worlds'

const PORT = 4196

let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT), viewport: { width: 1920, height: 1080 } })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})
test.describe.configure({ mode: 'serial' })

test('Dock: task tab and relay watch status', async ({ page, context }) => {
  const w = watch(page)
  await routes(context, { posts: [], preview: 'ok' })
  await storeSession(page)
  await open(page)
  await dockTab(page, /^任务$/)
  // the D1 mission tab is replaced on the sandbox surface
  await expect(page.locator('[data-slot="dock"] [role="tab"]').filter({ hasText: /^任务$/ })).toHaveCount(1)
  await expect(page.locator('[data-task-row="task-01"]')).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('[data-task-row="task-02"]')).toBeVisible()
  await expect(page.locator('[data-task-row="task-01"]')).toContainText('东区值守')
  await page.locator('[data-task-row="task-01"]').click()
  await expect(page.locator('[data-task-detail="task-01"]')).toContainText('task-01-w1')
  await expect(page.locator('[data-task-children="1"]')).toBeVisible()
  await expect(page.locator('[data-task-relay-kpi="task-02"]')).toContainText('被发现 0')
  for (const s of SIZES) {
    await page.setViewportSize({ width: s.w, height: s.h })
    await page.waitForTimeout(1200)
    await dockTab(page, /^任务$/)
    await shot(page, `m15-d2b-tasks-${s.w}x${s.h}`)
  }
  await page.setViewportSize({ width: 1920, height: 1080 })
  // relay tab: the statistics of relay/task-02/stats
  await dockTab(page, /^接力监控$/)
  const relay = page.locator('[data-relay-panel="task-02"]')
  await expect(relay).toBeVisible()
  await expect(relay.locator('[data-relay-kpi]')).toContainText('35.2')
  await expect(relay.locator('[data-relay-next="uav0002"]')).toBeVisible()
  const c0 = await relay.locator('[data-relay-countdown]').innerText()
  expect(c0).toMatch(/^1[89]:\d\d$/)
  await expect(relay.locator('[data-row-key="st-1"]')).toContainText('uav0001')
  await expect(relay.locator('[data-row-key="st-2"]')).toBeVisible()
  await expect(relay.locator('[data-relay-gaps]')).toBeVisible()
  await expect(relay.locator('[data-relay-target="person-01"]')).toBeVisible()
  for (const s of SIZES) {
    await page.setViewportSize({ width: s.w, height: s.h })
    await page.waitForTimeout(1200)
    await dockTab(page, /^接力监控$/)
    await shot(page, `m15-d2b-relay-${s.w}x${s.h}`)
  }
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors).toEqual([])
})

test('new task: polygon, pre-check, preview allocation and start with the preview id', async ({ page, context }) => {
  const w = watch(page)
  const s: Script = { posts: [], preview: 'ok' }
  await routes(context, s)
  await storeSession(page)
  await open(page)
  await dockTab(page, /^任务$/)
  await page.locator('[data-task-new-open]').click()
  await expect(page.locator('[data-task-new="type"]')).toBeVisible()
  await page.locator('[data-task-type="area_scan_gsd"]').click()
  await expect(page.locator('[data-task-drawing="area"]')).toBeVisible()
  await page.evaluate(() => (window as unknown as { __uxInject: { taskGeom: (p: number[][]) => void } }).__uxInject.taskGeom([[0, 0, 0], [300, 0, 0], [300, 250, 0], [0, 250, 0]]))
  await expect(page.locator('[data-task-params="area_scan_gsd"]')).toBeVisible()
  await expect(page.locator('[data-task-geom="4"]')).toContainText('0.075')
  await expect(page.locator('[data-task-start]')).toBeDisabled()
  await page.locator('[data-task-preview]').click()
  const card = page.locator('[data-task-preview-card="done"]')
  await expect(card).toBeVisible({ timeout: 10_000 })
  await expect(card).toHaveAttribute('data-task-preview-n', '2')
  await expect(card.locator('[data-task-why-n="makespan"]')).toContainText('12.8')
  await expect(card.locator('[data-task-scan]')).toContainText('110')
  await expect(card.locator('[data-task-footprint]')).toContainText('64')
  await expect(card.locator('[data-task-curve="4"] [data-lf-ref]')).toHaveCount(1)
  await expect(card.locator('[data-row-key="uav0002"]')).toBeVisible()
  for (const sz of SIZES) {
    await page.setViewportSize({ width: sz.w, height: sz.h })
    await shot(page, `m15-d2b-task-preview-${sz.w}x${sz.h}`)
  }
  await page.setViewportSize({ width: 1920, height: 1080 })
  const dry = s.posts.find((x) => x.url.includes('dry_run=true'))
  expect(dry?.body).toMatchObject({ type: 'area_scan_gsd', params: { gsd_cm: 3, polygon: [[0, 0], [300, 0], [300, 250], [0, 250]] } })
  await page.locator('[data-task-start]').click()
  await expect(page.locator('[data-task-new]')).toHaveCount(0, { timeout: 10_000 })
  const create = s.posts.find((x) => x.url === `POST /sessions/${SID}/tasks`)
  expect(create?.body).toMatchObject({ type: 'area_scan_gsd', preview_id: 'pv-abcdefgh' })
  expect(create?.headers['idempotency-key']).toMatch(/^task-/)
  await expect(page.locator('[data-slot="toast"]').filter({ hasText: 'task-03' })).toHaveCount(1)
  expect(w.errors).toEqual([])
})

test('new task: an infeasible preview explains the reason and offers to add a vehicle', async ({ page, context }) => {
  const w = watch(page)
  await routes(context, { posts: [], preview: 'infeasible' })
  await storeSession(page)
  await open(page)
  await page.keyboard.press('Control+k')
  await page.keyboard.type('新建任务')
  await page.keyboard.press('Enter')
  await page.locator('[data-task-type="area_guard"]').click()
  await page.evaluate(() => (window as unknown as { __uxInject: { taskGeom: (p: number[][]) => void } }).__uxInject.taskGeom([[0, 0, 0], [300, 0, 0], [300, 250, 0]]))
  await expect(page.locator('[data-task-gsd-req]')).toContainText('2.7')
  await page.locator('[data-task-preview]').click()
  const alert = page.locator('[data-task-infeasible="580"]')
  await expect(alert).toBeVisible({ timeout: 10_000 })
  await expect(alert.locator('[data-task-why="fleet_short"]')).toBeVisible()
  await expect(alert).toContainText('需要 3 架')
  await expect(page.locator('[data-task-start]')).toBeDisabled()
  await shot(page, 'm15-d2b-task-infeasible-1920x1080')
  await alert.locator('[data-task-add-vehicle]').click()
  await expect(page.locator('[data-add-vehicle]')).toBeVisible()
  await page.keyboard.press('Escape')
  expect(w.errors).toEqual([])
})

test('nests, catalogue and the GSD calculator read the domain stores', async ({ page, context }) => {
  const w = watch(page)
  await routes(context, { posts: [], preview: 'ok' })
  await storeSession(page)
  await open(page)
  await page.locator('[data-entity="nests"]').click()
  await expect(page.locator('[data-nest-row="nest-01"]')).toBeVisible({ timeout: 20_000 })
  await page.locator('[data-nest-row="nest-01"]').click()
  await expect(page.locator('[data-nest-status="nest-01"]')).toBeVisible()
  await expect(page.locator('[data-nest-chargers="4"]')).toBeVisible()
  await expect(page.locator('[data-nest-packs="2"]')).toBeVisible()
  for (const s of SIZES) {
    await page.setViewportSize({ width: s.w, height: s.h })
    await shot(page, `m15-d2b-nest-${s.w}x${s.h}`)
  }
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.locator('[data-nest-detail] button').first().click()
  await page.locator('[data-entity="drones"]').click()
  await page.locator('[data-fleet-catalog]').click()
  await expect(page.locator('[data-model-card]')).toHaveCount(2)
  await page.locator('[data-model-detail="p600_mid360"]').click()
  await expect(page.locator('[data-catalog-gsd="eo_gx40"]')).toBeVisible({ timeout: 10_000 })
  await shot(page, 'm15-d2b-catalog-1920x1080')
  await page.locator('[data-model-clone="p600_mid360"]').click()
  await expect(page.locator('[data-clone-form="p600_mid360"]')).toBeVisible()
  await page.keyboard.press('Escape')
  await page.locator('[data-catalog-sheet]').waitFor({ state: 'detached' }).catch(() => {})
  await page.keyboard.press('Control+k')
  await page.keyboard.type('GSD')
  await page.keyboard.press('Enter')
  const calc = page.locator('[data-gsd-calc]')
  await expect(calc).toBeVisible()
  await expect(calc.locator('[data-gsd-f]')).toHaveAttribute('data-gsd-f', '48.0')
  await expect(calc.locator('[data-gsd-p]')).toContainText('P(R)')
  await expect(calc.locator('[data-gsd-rmax]')).toContainText('R ')
  await expect(calc.locator('[data-gsd-window]')).toBeVisible()
  for (const s of SIZES) {
    await page.setViewportSize({ width: s.w, height: s.h })
    await shot(page, `m15-d2b-gsd-${s.w}x${s.h}`)
  }
  expect(w.errors).toEqual([])
})

test('FPV: sensor view bar and perception HUD on the real viewport', async ({ page, context }) => {
  test.skip(process.env.M15_UI_ONLY === '1', 'needs the real viewport (M06)')
  const w = watch(page)
  await routes(context, { posts: [], preview: 'ok' })
  test.skip(!(await serveWorlds(context)), 'worlds/ missing (make worlds)')
  await storeSession(page)
  await open(page, true)
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  await page.waitForTimeout(1500)
  await shot(page, 'm15-d2b-sandbox-viewport-1920x1080')
  await page.locator('[data-rail-row]').filter({ hasText: 'uav0001' }).first().click()
  await page.keyboard.press('Digit4')
  const bar = page.locator('[data-sensor-view-bar]')
  await expect(bar).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('[data-sensor-view]')).toHaveCount(4)
  await expect(page.locator('[data-sensor-view="eo"]')).toBeEnabled()
  const hud = page.locator('[data-fpv-hud]')
  await expect(hud).toBeVisible({ timeout: 20_000 })
  await expect(hud.locator('[data-fpv-px]')).toHaveAttribute('data-fpv-px', /\/14$/)
  await expect(hud.locator('[data-fpv-optics]')).toContainText('f 48.0 mm')
  for (const s of SIZES) {
    await page.setViewportSize({ width: s.w, height: s.h })
    await page.waitForTimeout(400)
    await shot(page, `m15-d2b-fpv-${s.w}x${s.h}`)
  }
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.locator('[data-sensor-view="eo"]').click()
  await expect(bar).toHaveAttribute('data-sensor-view-bar', 'eo')
  await expect(page.locator('[data-fpv-approx="eo"]')).toBeVisible()
  // the operator's zoom key sends uav/{id}/cmd/zoom (positive control of the __ux.rtCalls probe used by the viewer case)
  await page.keyboard.press('Equal')
  await expect.poll(() => page.evaluate(() => (window as unknown as { __ux: { rtCalls: string[] } }).__ux.rtCalls.some((c) => c.endsWith('/cmd/zoom'))),
    { timeout: 5000 }).toBe(true)
  await page.keyboard.press('KeyB')
  await expect(bar).toHaveAttribute('data-sensor-view-bar', /nir|fpv/)
  await page.waitForTimeout(400)
  await shot(page, 'm15-d2b-fpv-sensor-1920x1080')
  expect(w.errors).toEqual([])
})

// M15-AC-214 (functional part; M15-FR-220, D2-AC-37): a viewer of a run with the D2 capabilities (local surface, role
// viewer injected) uses the task and relay tabs, the GSD calculator and the FPV sensor views; the page issues no write
// request (no REST method other than GET or HEAD, the M04 world query and the connection token excepted, and no realtime
// call; __ux.rtCalls).
test('viewer: the D2 read-only interactions issue no write request', async ({ page, context }) => {
  const w = watch(page)
  const writes: string[] = []
  page.on('request', (r) => {
    const m = r.method()
    const u = new URL(r.url())
    // reads: the M04 world query (ray_hit is a POST) and the connection token of the realtime link (D1 auth, no run state)
    if (m === 'GET' || m === 'HEAD' || m === 'OPTIONS' || /\/api\/world\/[^/]+\/query$/.test(u.pathname) || u.pathname === '/api/auth/token') return
    writes.push(`${m} ${u.pathname}`)
  })
  await routes(context, { posts: [], preview: 'ok' })
  const worlds = await serveWorlds(context)
  await page.goto(`/world/synthcity?source=fake&fakeFixture=/__d2/sandbox_t5.awrrt&fakeLoop=1&reveal=shell${worlds ? '' : '&viewport=off'}`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 60_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
  await injectConn(page, { role: 'viewer', seat: null, caps: ['targets', 'perception', 'tasking', 'calendar'] })

  await dockTab(page, /^任务$/)
  await expect(page.locator('[data-slot="dock"]')).toBeVisible()
  await expect(page.locator('[data-task-new-open]')).toHaveCount(0)
  await dockTab(page, /^接力监控$/)
  await expect(page.locator('[data-relay-panel]')).toBeVisible()

  await page.keyboard.press('Control+k')
  await page.keyboard.type('GSD')
  await page.keyboard.press('Enter')
  await expect(page.locator('[data-gsd-calc]')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.locator('[data-gsd-calc]')).toHaveCount(0)

  if (worlds) {
    await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
    await page.locator('[data-rail-row]').filter({ hasText: 'uav0001' }).first().click()
    await page.keyboard.press('Digit4')
    const bar = page.locator('[data-sensor-view-bar]')
    await expect(bar).toBeVisible({ timeout: 20_000 })
    // operator controls are absent for a viewer (zoom rail, gimbal menu); the views are local display state
    await expect(page.locator('[data-fpv-zoom]')).toHaveCount(0)
    await expect(page.locator('[data-fpv-gimbal]')).toHaveCount(0)
    for (const v of ['eo', 'nir', 'lwir', 'fpv'] as const) {
      const b = page.locator(`[data-sensor-view="${v}"]`)
      if (!(await b.isEnabled())) continue
      await b.click()
      // the thermal palette is local display state for a viewer
      const black = page.locator('[data-fpv-palette]').getByText('黑热')
      if (v === 'lwir' && (await black.count())) await black.click()
    }
    await page.keyboard.press('KeyB')
    await page.keyboard.press('KeyI')
    await page.keyboard.press('Equal')
  }
  await page.waitForTimeout(800)
  const calls = await page.evaluate(() => (window as unknown as { __ux: { rtCalls: string[] } }).__ux.rtCalls.slice())
  console.log(`[m15-viewer] rt calls: ${JSON.stringify(calls)}; writes: ${JSON.stringify(writes)}`)
  expect(calls).toEqual([])
  expect(writes).toEqual([])
  expect(w.errors).toEqual([])
})
