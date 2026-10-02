// FX-WEB2 UI functional cases on a test build with FakeSource and mocked REST (no backend; `?viewport=off&reveal=shell`
// like the other M15 specs):
//   brand       page title and header lock-up say "ANet Drone4D" (ADR-056); About names the licence, the repository,
//               the UrbanScene3D source with "research use" and the copyright line (LICENSE 1b; M16-FR-006);
//   timeline    the bar has the readout, the overview and detail tracks and the live rate table; markers of the M12
//               track model show a hover tooltip; the context menu offers "add a bookmark here"; the Timeline tab has the
//               legend, the facts and the bookmark list;
//   jobs        /jobs lists R39 rows with progress and the scale_status badge (relative with its warning); 60 job.progress
//               events in 1.5 s cause at most 4 store writes per second (D1-AC-22 "progress <= 4 Hz"); the new-job dialog
//               posts a Mock request (R38); the detail sheet shows the stages, the alignment and the product world;
//   runs        /runs lists R33 rows, greys the recording run and asks before replaying;
//   report      /reports?src= renders the fidelity block and the data-source footer from report.meta.json;
// every page: no D1-AC-20 glyphs, no page errors. Screenshots: .cache/impl/shots/fx-web2-ui-*.png.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, glyphScan, isTestBuild, shot, startPreview, watch } from './helpers/shell'

const PORT = 4193
const SAMPLE = readFileSync(join(import.meta.dirname, 'fixtures', 'report-sample.json'), 'utf8')
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT) })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})
test.describe.configure({ mode: 'serial' })

const JOB = 'j-01a0ed3d-5212-7fa7-982d-7bc5dd4430de'
const jobItem = (over: Record<string, unknown> = {}) => ({
  job_id: JOB, kind: 'recon', state: 'INFERRING', stage: 'INFERRING', progress_pct: 23, submitted_by: 'p-1',
  created_unix_ns: String(Date.now() * 1e6 - 60e9), updated_unix_ns: String(Date.now() * 1e6), target_world_id: 'shenzhen-recon-01',
  attempt: 1, resumable: false, scale_status: 'relative', error: null,
  recon: { engine: 'mock', session_id: 's-1', source_world_id: 'shenzhen', target_world_id: 'shenzhen-recon-01', scale_status: 'relative',
    alignment: { method: 'none', status: 'rejected', inlier_ratio: null, rmse_m: null, needs_review: true }, frames: { done: 140, total: 600 },
    paused_reason: null, stage_durations_s: {} },
  ...over,
})

async function mockRest(page: Page): Promise<{ posts: unknown[] }> {
  const posts: unknown[] = []
  await page.route('**/api/jobs?**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [jobItem()], next_cursor: null }) }))
  await page.route(`**/api/jobs/${JOB}/log**`, (r) => r.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ lines: [JSON.stringify({ t_wall_ns: String(Date.now() * 1e6), job_id: JOB, level: 'info', msg: 'stage INFERRING start' })] }) }))
  await page.route('**/api/recon/jobs', (r) => {
    posts.push(r.request().postDataJSON())
    return r.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: 'j-01a0ed3d-5212-7fa7-982d-7bc5dd4430df', state: 'QUEUED', target_world_id: 'shenzhen-recon-02' }) })
  })
  await page.route('**/api/runs?**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ next_cursor: null, items: [
    { run_id: 'r20260929-124038-5ae3', world_id: 'shenzhen', created_unix_ns: String(Date.now() * 1e6), keep: false, bytes: 7e6, scenario: 's1-shenzhen-facade',
      compatible: true, current: true, segments: [{ seg: 0, state: 'OPEN', closed: false, t0_ns: 0, t1_ns: 331e9, bytes: 7e6, speed_max: 20, epochs: [] }] },
    { run_id: 'r20260929-100000-f00d', world_id: 'shenzhen', created_unix_ns: String(Date.now() * 1e6 - 3.6e12), keep: false, bytes: 6.7e6, scenario: null,
      compatible: true, current: false, segments: [{ seg: 0, state: 'CLOSED', closed: true, t0_ns: 0, t1_ns: 150e9, bytes: 6.7e6, speed_max: 20, epochs: [] }] },
    { run_id: 'r20260928-090000-beef', world_id: 'suzhou', created_unix_ns: String(Date.now() * 1e6 - 8.6e13), keep: true, bytes: 1.2e7, scenario: null,
      compatible: false, current: false, segments: [{ seg: 0, state: 'CLOSED', closed: true, t0_ns: 0, t1_ns: 600e9, bytes: 1.2e7, speed_max: 12.5, epochs: [] }] },
  ] }) }))
  return { posts }
}

async function open(page: Page, path = '/world/shenzhen'): Promise<void> {
  await page.goto(`${path}${path.includes('?') ? '&' : '?'}source=fake&fakeN=2&reveal=shell&viewport=off`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 45_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
}

const spa = (page: Page, to: string) => page.evaluate((u) => {
  history.pushState(null, '', u)
  dispatchEvent(new PopStateEvent('popstate'))
}, to)

test('brand name, About content and the synthetic-source honesty badge', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1920, height: 1080 })
  await open(page)
  await expect(page.locator('[data-brand="lockup"]')).toContainText('ANet Drone4D')
  await expect(page.locator('[data-brand-avatar]')).toBeVisible()
  expect(await page.title()).toContain('ANet Drone4D')
  await expect(page.locator('[data-badge="synthetic-source"]')).toBeVisible()
  await page.getByRole('menuitem', { name: '帮助' }).click()
  await page.getByRole('menuitem', { name: '关于' }).click()
  const about = page.locator('[data-about]')
  await expect(about).toBeVisible()
  await expect(about.locator('[data-brand="badge"]')).toBeVisible()
  await expect(about.locator('[data-about-product]')).toHaveText('ANet Drone4D')
  await expect(about.locator('[data-about-license]')).toContainText('ANet Open Source License')
  await expect(about.getByRole('link', { name: /ANetResearch\/ANet-Drone4D/ })).toHaveAttribute('href', 'https://github.com/ANetResearch/ANet-Drone4D')
  await expect(about.locator('[data-about-data]')).toContainText('UrbanScene3D')
  await expect(about.locator('[data-about-data]')).toContainText('科研')
  await expect(about.locator('[data-about-copyright]')).toContainText('Copyright (c) 2026 Agent Network Research')
  expect(await glyphScan(page)).toEqual([])
  await shot(page, 'fx-web2-ui-about')
  expect(w.errors).toEqual([])
})

test('timeline bar, track hover, context menu and the Timeline tab', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1920, height: 1080 })
  await mockRest(page)
  await open(page)
  const bar = page.locator('[data-slot="timeline-bar"]')
  await expect(bar.locator('[data-timeline-readout]')).toContainText('SIM')
  await expect(bar.locator('[data-figure="timeline-overview"]')).toBeVisible()
  await expect(bar.locator('[data-figure="timeline-track"]')).toBeVisible()
  await expect(bar.locator('[data-rate-toggle] [data-slot="toggle-group-item"]')).toHaveCount(6)
  await expect(bar.locator('[data-slot="badge"][data-mode="live"]')).toHaveText('LIVE')
  // markers of the M12 track model at the current view: one warning and one lifecycle mark
  type TlWin = { __timeline: { store: { getState(): { view: { t0S: number; t1S: number } } }; track: { addMarker(t: number, l: number, m: number, a: number, s: number): boolean } } }
  const tMarkS = await page.evaluate(() => {
    const tl = (window as unknown as TlWin).__timeline
    const v = tl.store.getState().view
    const t = (v.t0S + v.t1S) / 2
    tl.track.addMarker(t * 1000, 2, 3, 0xffff, 42)
    tl.track.addMarker(t * 1000 + 50, 0, 1, 0xffff, 43)
    return t
  })
  const det = bar.locator('[data-timeline-detail]')
  const box = (await det.boundingBox())!
  const tip = bar.locator('[data-timeline-hover]')
  // the live view follows the clock (about 25 px/s at 1920 px over a 60 s span): hover where the marker is now, and again
  // when the view moved past the 6 px hit tolerance before the pointer arrived (flaky in a loaded batch otherwise)
  let nudge = 0
  await expect.poll(async () => {
    const f = await page.evaluate((t) => {
      const v = (window as unknown as TlWin).__timeline.store.getState().view
      return (t - v.t0S) / (v.t1S - v.t0S)
    }, tMarkS)
    nudge = 1 - nudge
    await page.mouse.move(box.x + box.width * f, box.y + box.height / 3 + nudge)
    return (await tip.isVisible()) ? await tip.innerText() : ''
  }, { timeout: 10_000, intervals: [100, 250, 500] }).toContain('个事件')
  await expect(tip).toContainText('警告')
  await shot(page, 'fx-web2-ui-timeline-hover')
  await page.mouse.click(box.x + box.width * 0.2, box.y + box.height / 3, { button: 'right' })
  await expect(page.getByRole('menuitem', { name: '在此处添加书签' })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: '适配全段' })).toBeVisible()
  await page.keyboard.press('Escape')
  // the Timeline tab of the Dock
  await page.mouse.move(960, 400)
  await page.keyboard.press('Backquote')
  await page.locator('[data-slot="dock"] [role="tab"]', { hasText: '时间轴' }).click()
  const panel = page.locator('[data-panel-timeline]')
  await expect(panel).toBeVisible()
  await expect(panel.locator('[data-timeline-legend]')).toContainText('航线变更')
  await expect(panel.locator('[data-timeline-facts]')).toContainText('所见时刻')
  await expect(panel.locator('[data-bookmarks-empty]')).toBeVisible()
  expect(await glyphScan(page)).toEqual([])
  await shot(page, 'fx-web2-ui-timeline-tab')
  expect(w.errors).toEqual([])
})

test('jobs page: rows, <= 4 Hz progress writes, new job and the detail sheet', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1920, height: 1080 })
  const rest = await mockRest(page)
  await open(page)
  await spa(page, '/jobs')
  const row = page.locator(`[data-row-key="${JOB}"]`)
  await expect(row).toBeVisible({ timeout: 15_000 })
  await expect(row.locator('[data-job-state="INFERRING"]')).toContainText('推理')
  await expect(row.locator('[data-scale-status="relative"]')).toHaveText('相对尺度')
  // 60 progress events over 1.5 s: the store is written at most 4 times per second (+ the trailing flush)
  const writes = await page.evaluate(async (id) => {
    const j = (window as unknown as { __jobs: { store: { getState(): { flushes: number } }; ingest(b: unknown[]): void } }).__jobs
    const f0 = j.store.getState().flushes
    for (let i = 0; i < 60; i++) {
      j.ingest([{ type: 'job.progress', data: { job_id: id, state: 'INFERRING', stage: 'INFERRING', progress_pct: 24 + i, frames_done: 150 + i, frames_total: 600, fps: 8.2, attempt: 1 } }])
      await new Promise((ok) => setTimeout(ok, 25))
    }
    await new Promise((ok) => setTimeout(ok, 400))
    return j.store.getState().flushes - f0
  }, JOB)
  expect(writes).toBeGreaterThanOrEqual(2)
  expect(writes).toBeLessThanOrEqual(8)
  await expect(row.locator('[data-job-progress]')).toHaveAttribute('data-job-progress', '83')
  await shot(page, 'fx-web2-ui-jobs')
  // new job (R38 body: engine mock, world_sample source, frames)
  await page.locator('[data-job-new-open]').click()
  await expect(page.locator('[data-job-new]')).toBeVisible()
  await page.locator('#job-target').fill('Bad Id')
  await expect(page.locator('[data-job-submit]')).toBeDisabled()
  await page.locator('#job-target').fill('shenzhen-recon-02')
  await page.locator('[data-job-submit]').click()
  await expect(page.locator('[data-job-new]')).toHaveCount(0)
  expect(rest.posts).toHaveLength(1)
  expect(rest.posts[0]).toMatchObject({ engine: 'mock', source: { kind: 'world_sample', world_id: 'shenzhen', path: 'helix', frames: 600 }, target_world_id: 'shenzhen-recon-02' })
  // detail sheet
  await row.click()
  const sheet = page.locator(`[data-job-sheet="${JOB}"]`)
  await expect(sheet).toBeVisible()
  await expect(sheet.locator('[data-figure="job-stages"]')).toContainText('地理配准')
  await expect(sheet).toContainText('尺度未知，距离与高度不可用于物理')
  await expect(sheet).toContainText('需要复核')
  await expect(sheet.locator('[data-job-log]')).toContainText('INFO stage INFERRING start')
  expect(await glyphScan(page)).toEqual([])
  await shot(page, 'fx-web2-ui-jobs-sheet')
  expect(w.errors).toEqual([])
})

test('runs page: compatibility, the recording run and the replay confirmation', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1920, height: 1080 })
  await mockRest(page)
  await open(page)
  await spa(page, '/runs')
  await expect(page.locator('[data-view="runs"]')).toBeVisible()
  await expect(page.locator('[data-run-replay="r20260929-124038-5ae3"]')).toHaveAttribute('aria-disabled', 'true')
  await expect(page.locator('[data-run-replay="r20260928-090000-beef"]')).toHaveAttribute('aria-disabled', 'true')
  const ok = page.locator('[data-run-replay="r20260929-100000-f00d"]')
  await expect(ok).not.toHaveAttribute('aria-disabled', 'true')
  await ok.click()
  await expect(page.locator('[data-confirm="replay:open"]')).toContainText('所有已连接的客户端都会看到回放画面')
  await shot(page, 'fx-web2-ui-runs-confirm')
  await page.keyboard.press('Escape')
  expect(await glyphScan(page)).toEqual([])
  expect(w.errors).toEqual([])
})

test('report fidelity block and data-source footer from report.meta.json', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  const meta = {
    datasets: { shenzhen: { name: 'UrbanScene3D', version: 'virtual_cities-sampled (GitHub Release v0.0.1)', citation: 'Lin et al., Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset, ECCV 2022', anchor: 'synthetic' } },
    fidelity: { backend: 'Mock L1（PX4-lite）', vehicle: 'p600_mid360', vehicle_status: '参数未辨识', simulated: true, synthetic_source: false },
  }
  await page.route('**/fx/report.json', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: SAMPLE }))
  await page.route('**/fx/report.meta.json', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(meta) }))
  await page.goto('/reports?src=/fx/report.json&viewport=off')
  await expect(page.locator('[data-view="report"]')).toHaveAttribute('data-report-ready', 'true', { timeout: 30_000 })
  const fid = page.locator('[data-report-fidelity]')
  await expect(fid).toContainText('Mock L1')
  await expect(fid).toContainText('参数未辨识')
  await expect(fid).toContainText('simulated')
  const foot = page.locator('[data-report-footer]')
  await expect(foot).toContainText('UrbanScene3D（Lin et al., ECCV 2022）')
  await expect(foot).toContainText('科研用途')
  await expect(foot).toContainText('示意锚点')
  expect(await glyphScan(page)).toEqual([])
  await shot(page, 'fx-web2-ui-report-footer', true)
  expect(w.errors).toEqual([])
})
