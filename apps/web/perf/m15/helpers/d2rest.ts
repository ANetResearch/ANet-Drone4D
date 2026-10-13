// Scripted sandbox REST of the WP-12b specs (tasks-d2, shots-d2): the contracts fixtures (fixtures/d2/rest) plus the task
// list, plan geometry, dry-run preview and create replies, the catalogue (models, sensors, detail), the tasking snapshot and
// a flat-ground M04 ray_hit (z = 0) for the viewport tools;
// the realtime link is FakeSource replaying fixtures/d2/sandbox_t5.awrrt (served by a route). Helpers open the sandbox
// route with a stored session token and switch Dock tabs.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import { injectConn, isTestBuild } from './shell'

export const REPO = join(import.meta.dirname, '..', '..', '..', '..', '..')
const FIX = join(REPO, 'packages', 'contracts', 'fixtures', 'd2')
const rest = (name: string): Record<string, unknown> => JSON.parse(readFileSync(join(FIX, 'rest', `${name}.json`), 'utf8')) as Record<string, unknown>
const AWRRT = readFileSync(join(FIX, 'sandbox_t5.awrrt'))
export const SID = 'sb-k3m7q2x7ab'
export const TOKEN = 'v1.fixture.0000000000000000'
const future = () => String(BigInt(Date.now() + 1_800_000) * 1_000_000n)
export const SIZES = [{ w: 1920, h: 1080 }, { w: 1280, h: 720 }] as const

const TASKS = [
  { tid: 'task-01', type: 'area_guard', label: '东区值守', state: 'RUNNING', revision: 1, children: ['task-02'], parent: null,
    params: { polygon: [[-200, -150], [250, -150], [250, 200], [-200, 200]], target_class: 'person', level_req: 'R', revisit_s: 300 }, common: { on_detect: 'relay' } },
  { tid: 'task-02', type: 'relay_watch', label: '接力 人-01', state: 'RUNNING', revision: 1, children: [], parent: 'task-01', params: { target: 'person-01', level_req: 'R' } },
]
const PLANS: Record<string, unknown> = {
  'task-01': { revision: 1, geometry: { subregions: [[[-200, -150], [250, -150], [250, 200], [-200, 200]]], legs: [{ uav: 'uav0001', kind: 'patrol', polyline: [[-180, -130, 60], [230, -130, 60], [230, 180, 60], [-180, 180, 60], [-180, -130, 60]] }] },
    assignments: [{ uav: 'uav0001', nest: 'nest-01', items: [{ wid: 'task-01-w1', kind: 'guard_cycle', ref: 'g1' }] }] },
  'task-02': { revision: 1, geometry: { stations: [{ id: 'st-1', pos: [80, -360, 70], target: 'person-01', role: 'primary', uav: 'uav0001' }, { id: 'st-2', pos: [430, -60, 70], target: 'person-01', role: 'handover', uav: null }] },
    assignments: [{ uav: 'uav0001', nest: 'nest-01', items: [{ wid: 'task-02-w1', kind: 'relay_station', ref: 'st-1' }] }] },
}
const PREVIEW = {
  preview_id: 'pv-abcdefgh', state: 'done', n_vehicles: 2, makespan_s: 480, t_max_s: 600, why_n: { rule: 'makespan', n_minus_1_makespan_s: 768 },
  makespan_curve: [{ n: 1, makespan_s: 768 }, { n: 2, makespan_s: 480 }, { n: 3, makespan_s: 372 }, { n: 4, makespan_s: 294 }],
  scan: { h_m: 110, f_mm: 10.6, strip_w_m: 57.6, spacing_m: 51.8, n_strips: 8, theta_e_deg: 15, footprint_bound_m: 64 },
  coverage: { direct_pct: 97.5, visible_pct: 100, fill_waypoints: 6, obstacle_pct: 0.24 },
  assignments: [{ uav: 'uav0001', nest: 'nest-01', items: [{ wid: 'w1', kind: 'strip_group', ref: 's1-4' }], eta_s: 470 },
    { uav: 'uav0002', nest: 'nest-01', items: [{ wid: 'w2', kind: 'strip_group', ref: 's5-8' }, { wid: 'w3', kind: 'fill', ref: 'f1' }], eta_s: 480 }],
  geometry: { subregions: [[[0, 0], [300, 0], [300, 250], [0, 250]]], strips: [{ id: 's1', p0: [10, 20, 110], p1: [290, 20, 110], uav: 'uav0001' }, { id: 's2', p0: [10, 70, 110], p1: [290, 70, 110], uav: 'uav0001' }] },
  sustainability: { verdict: 'not_applicable' }, warnings: ['multi_sortie'],
}

export interface Script { posts: { url: string; body: unknown; headers: Record<string, string> }[]; preview: 'ok' | 'infeasible' }

export async function routes(ctx: BrowserContext, s: Script): Promise<void> {
  const session = () => ({ ...rest('session'), expires_unix_ns: future() })
  await ctx.route('**/__d2/sandbox_t5.awrrt', (r) => r.fulfill({ status: 200, body: AWRRT, headers: { 'content-type': 'application/octet-stream', 'cross-origin-resource-policy': 'same-origin' } }))
  // M04 ray_hit on a flat ground (z = 0): the viewport tools pick the ground through the world query service, absent here
  await ctx.route('**/api/world/*/query', (r) => {
    const q = (r.request().postDataJSON() ?? {}) as { op?: string; origin_enu_m?: number[]; dir?: number[] }
    const o = q.origin_enu_m ?? [0, 0, 0]
    const d = q.dir ?? [0, 0, -1]
    const t = d[2] < -1e-6 ? -o[2] / d[2] : -1
    const hit = q.op === 'ray_hit' && t > 0
    return r.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify(hit
      ? { hit: true, point_enu_m: [o[0] + t * d[0], o[1] + t * d[1], 0], dist_m: t, surface: 'dtm', ground_z_m: 0 }
      : { hit: false, point_enu_m: null, dist_m: null, surface: 'none' }) })
  })
  await ctx.route('**/api/sandbox/v1/**', async (r) => {
    const req = r.request()
    const url = new URL(req.url())
    const p = url.pathname.replace('/api/sandbox/v1', '')
    const m = req.method()
    const json = (status: number, body: unknown) => r.fulfill({ status, body: JSON.stringify(body), headers: { 'content-type': 'application/json' } })
    if (m !== 'GET') s.posts.push({ url: `${m} ${p}${url.search}`, body: req.postDataJSON() as unknown, headers: req.headers() })
    if (m === 'GET' && p === '/worlds') return json(200, rest('worlds'))
    if (m === 'GET' && p === '/templates') return json(200, rest('templates'))
    if (m === 'GET' && p === '/meta') return json(200, rest('meta'))
    if (m === 'GET' && p === '/catalog/models') return json(200, { items: [
      { model_id: 'p600_mid360', name_zh: 'P600', category: 'multirotor', reference: false, version: '2', mass_kg: 3.825, mtow_kg: 4.1, endurance: { hover_s: 1740 }, speed: { cruise_mps: 8, max_mps: 15 }, link_range_m: 3500, default_sensors: ['eo_gx40', 'mid360'] },
      { model_id: 'awr_f1', name_zh: 'AWR-F1', category: 'fixed_wing', reference: true, version: '1', mass_kg: 6.2, endurance: { cruise_s: 7200 }, speed: { cruise_mps: 20, max_mps: 30, min_mps: 14 }, link_range_m: 15000, default_sensors: ['eo_gx40'] }] })
    if (m === 'GET' && p === '/catalog/sensors') return json(200, { items: [{ sensor_type: 'eo_gx40', name_zh: 'GX40', kind: 'eo', reference: false, spec: { pixel_um: 1.45, native_w_px: 3840, native_h_px: 2160, f_mm: [4.8, 48], f_number: [1.7, 3.2], outputs: [{ name: '1080p', w_px: 1920, crop_w_px: 3840, crop_h_px: 2160 }, { name: '4k', w_px: 3840, crop_w_px: 3840, crop_h_px: 2160 }] } }] })
    if (m === 'GET' && p.startsWith('/catalog/models/')) return json(200, { model_id: 'p600_mid360', name_zh: 'P600', category: 'multirotor', reference: false, version: '2', default_sensors: ['eo_gx40', 'mid360'],
      params_table: [{ group: '飞行器', key: 'mass', label_zh: '起飞质量', value: 3.825, unit: 'kg', confidence: 'A', use_zh: '动力学' }] })
    if (m === 'GET' && p === `/sessions/${SID}`) return json(200, session())
    if (m === 'GET' && p === `/sessions/${SID}/targets`) return json(200, rest('targets'))
    if (m === 'GET' && p === `/sessions/${SID}/nests`) return json(200, rest('nests'))
    if (m === 'GET' && p === `/sessions/${SID}/models`) return json(200, { items: [] })
    if (m === 'GET' && p === `/sessions/${SID}/tasks`) return json(200, { items: TASKS })
    if (m === 'GET' && p.startsWith(`/sessions/${SID}/tasks/`) && url.searchParams.get('include') === 'plan') {
      const tid = p.split('/').pop() ?? ''
      const o = TASKS.find((x) => x.tid === tid)
      return o ? json(200, { ...o, plan: PLANS[tid] }) : json(404, { code: 104, message: 'not found' })
    }
    if (m === 'GET' && p === `/sessions/${SID}/tasking/snapshot`) return json(200, { revision: 3, keepouts: [{ id: 'person-01', center_enu_m: [150, -40, 0], r_m: 320 }] })
    if (m === 'POST' && p === `/sessions/${SID}/tasks` && url.searchParams.get('dry_run') === 'true') {
      if (s.preview === 'infeasible') return json(422, { code: 580, message: 'infeasible', detail: { why: 'fleet_short', n_required: 3, shortfall: 1, remedies: [{ kind: 'fleet_short', text_zh: '增加同类机，或开启 allow_multi_sortie' }], preview_id: 'pv-x' } })
      return json(200, PREVIEW)
    }
    if (m === 'POST' && p === `/sessions/${SID}/tasks`) return json(201, { tid: 'task-03', type: 'area_scan_gsd', label: null, state: 'PLANNING', revision: 0, params: {} })
    if (m === 'POST' && p === `/sessions/${SID}/keepalive`) return json(200, { session: session(), renewed: true, token: TOKEN, token_exp_unix_ns: future() })
    return json(404, { code: 305, message: 'not scripted' })
  })
}

const fake = (viewport: boolean) => `?source=fake&fakeFixture=/__d2/sandbox_t5.awrrt&fakeLoop=1&reveal=shell${viewport ? '' : '&viewport=off'}`
export async function storeSession(page: Page): Promise<void> {
  await page.addInitScript(([sid, token, exp]) => {
    sessionStorage.setItem('awr.sandbox.session.v1', JSON.stringify({ sid, token, token_exp_unix_ms: exp, world: 'synthcity', template: 't5_area_guard' }))
    localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 }))
  }, [SID, TOKEN, Date.now() + 1_800_000] as const)
}
export async function open(page: Page, viewport = false): Promise<void> {
  await page.goto(`/sandbox/${SID}${fake(viewport)}`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 60_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 20_000 })
  // the fixture's serverInfo carries no role: the sandbox principal is the operator with the seat preset (ADR-093)
  await injectConn(page, { role: 'operator', seat: 'held' })
}
export async function dockTab(page: Page, name: RegExp): Promise<void> {
  const host = page.locator('[data-slot="rail-host"][data-side="bottom"]')
  const tab = page.locator('[data-slot="dock"] [role="tab"]').filter({ hasText: name })
  // after a viewport resize the breakpoint layout applies its defaults a moment later (the presets below 2200 px fold the
  // Dock; a folding Dock still reads as visible during its transition): decide on the host state once it settled, and
  // open it again when a late relayout folded it after the check
  await page.waitForTimeout(1200)
  for (let i = 0; i < 4; i++) {
    if ((await host.getAttribute('data-state')) !== 'open') {
      await expect(page.locator('[data-slot="sheet-content"],[data-slot="dialog-content"],[data-slot="alert-dialog-content"]')).toHaveCount(0, { timeout: 5000 })
      await page.getByRole('button', { name: '显示底部面板' }).click()
      await expect(host).toHaveAttribute('data-state', 'open')
    }
    if (await tab.waitFor({ state: 'visible', timeout: 2000 }).then(() => true, () => false)) break
    await page.waitForTimeout(600)
  }
  await tab.click({ timeout: 10_000 })
}

