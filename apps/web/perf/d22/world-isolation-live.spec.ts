// World isolation on the real stack (D2.2-W1, M15 with M06, M11, M18, M19, M20; ADR-111; AWR-14 §20.2.3). The shared show
// runs scenario S7 (synthcity relay: 8 vehicles, 3 targets, 2 nests, an area guard task) on tests/e2e/sbstack.py behind the
// nginx-like preview of perf/d21/live21.ts; every page runs under the strict WebGL2 minimum limits (24 / 12 / 12 / 24,
// 16 384 B; perf/d21/limits.ts, the rules of perf/m06/gpu-limits.spec.ts) and must raise no WebGL error, no three warning
// and no page error.
// D2.2-W2 (lib/demo CITY_SHOW_FIXTURE, tools/sim/gen_city_watch.py and capture_city_watch.py): shenzhen, suzhou and
// newyork now each carry their own recorded monitoring show (a `fake:` replay of a captured D2.2-W2 run: two nests,
// seven vehicles, seven targets, two tasks), so the city checks below assert "only this city's own entities, with its
// own counts" instead of D2.2-W1's "everything stays empty while browsing another city"; a world with no recorded show
// still falls back to the D2.2-W1 static surface (apps/web/tests/m15/surfaceRoutes.test.ts covers that fallback at the
// unit level, not repeated here).
//   1. public demo build (the reported path): /world/synthcity?sel=<vehicle> on the shared show, the vehicle selected, the
//      acoustic field view with all perception volumes, a target's detail with its readout card; then an in-app switch to
//      shenzhen, suzhou and newyork: the surface badge reads "shared" with a `data-city-replay` mark, exactly seven
//      vehicle rows and seven target rows (D2.2-W2's own counts) and no residual synthcity run id or `sel`; back on
//      synthcity the show is displayed again (vehicles, targets, legend) and a vehicle can be selected;
//   2. test build (the local surface: the link stays on the show while the route shows another city), PerfGovernor held
//      (window.__vp.governorHold) so frustums stay on under SwiftShader: window.__vp.isolation() proves the data layer,
//      not only the panels: the selected vehicle's frustum is drawn on synthcity; switching to a city with a recorded
//      show fills the data layer with that city's own counts (not synthcity's: the scope is no longer foreign, the
//      stream world is the city); back on synthcity they fill again and the vehicle's frustum is drawn once selected.
// Builds: D22_DEMO_DIST (VITE_AWR_DEMO=public) and D22_TEST_DIST (VITE_AWR_TEST_SWITCHES=1), otherwise private builds in
// .cache/d22/<kind> (rebuilt when missing or with D22_REBUILD=1). Port offset: AWR_PORT_OFFSET (preview ports + 10 x offset).
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { emulate, MIN } from '../d21/limits'
import { baseUrl, BUILD_ENV, REPO, revealed, shot, skipOnboarding, startLive21, startLive21Preview, watch, WEB, type Live21, type Watch } from '../d21/live21'

const DEMO_PORT = 4231
const TEST_PORT = 4232
const CITIES = ['shenzhen', 'suzhou', 'newyork'] as const
const SHOW = 'synthcity'
/** D2.2-W2 gen_city_watch.py: every city's recorded show has exactly these counts (two nests, two tasks) */
const N_CITY_VEHICLES = 7
const N_CITY_TARGETS = 7
/** the vehicle of the report (`?sel=p600-03`): a P600 of nest A with an EO camera, its frustum is drawn while selected (the AWR-V1
 *  roster model `awr-v1` has no entry in the sensor specs keyed `awr_v1`, so V1 vehicles show no frustum: see the D2.2-W1 report) */
const VEHICLE = process.env.D22_VEHICLE ?? 'p600-03'

function ensureBuild(kind: 'demo' | 'test'): string {
  const given = kind === 'demo' ? process.env.D22_DEMO_DIST : process.env.D22_TEST_DIST
  if (given) return given
  const out = join(REPO, '.cache', 'd22', kind)
  if (existsSync(join(out, 'index.html')) && process.env.D22_REBUILD !== '1') return out
  const env: NodeJS.ProcessEnv = { ...process.env, ...BUILD_ENV }
  delete env.VITE_AWR_TEST_SWITCHES
  delete env.VITE_AWR_DEMO
  if (kind === 'test') {
    env.M15_DIST = out
    execFileSync('node', [join(WEB, 'perf', 'm15', 'helpers', 'build-ui.mjs')], { cwd: WEB, env, stdio: 'pipe' })
  } else {
    env.VITE_AWR_DEMO = 'public'
    execFileSync('npx', ['vite', 'build', '--outDir', out, '--emptyOutDir', '--logLevel', 'warn'], { cwd: WEB, env, stdio: 'pipe' })
  }
  return out
}

let live: Live21 | null = null
const servers: PreviewServer[] = []
const dists = { demo: '', test: '' }

test.describe.configure({ mode: 'serial' })
test.use({ viewport: { width: 1600, height: 900 }, locale: 'zh-CN' })
test.beforeAll(async () => {
  test.setTimeout(900_000)
  dists.demo = ensureBuild('demo')
  dists.test = ensureBuild('test')
  live = await startLive21({ maxSessions: 1, set: ['run.scenario=s7-synthcity-relay', 'run.scenario_profile=public'] })
  servers.push(await startLive21Preview(DEMO_PORT, live, dists.demo), await startLive21Preview(TEST_PORT, live, dists.test))
})
test.afterAll(async () => {
  for (const s of servers) await s.close()
  await live?.stop()
})

async function open(page: Page, url: string): Promise<Watch> {
  const w = watch(page)
  await page.addInitScript(emulate, { L: MIN, renderer: null, mobile: null })
  await skipOnboarding(page)
  await page.goto(url)
  await revealed(page)
  return w
}

/** in-app navigation, as the World Hub, the world menu and the world panel do (router.navigate: pushState + resolve) */
async function goWorld(page: Page, world: string): Promise<void> {
  await page.evaluate((w) => {
    history.pushState(null, '', `/world/${w}`)
    dispatchEvent(new PopStateEvent('popstate'))
  }, world)
}

const perfDraws = (page: Page): Promise<{ drones: number; frustums: number }> => page.evaluate(() => {
  const L = (window as unknown as { __perf: { layers: Record<string, { draws: number }> } }).__perf.layers
  return { drones: L.drones.draws, frustums: L.frustums.draws }
})

/**
 * the frustum of the selected vehicle is drawn on the show, unless the PerfGovernor already switched frustums off (step 2:
 * on SwiftShader the point-cloud budget sits at its floor and the governor walks its steps; a step whose layer draws
 * nothing at that moment is taken at once): then the case records it and relies on the data-layer checks
 */
async function expectFrustum(page: Page): Promise<void> {
  const deadline = Date.now() + 60_000
  for (;;) {
    const d = await perfDraws(page)
    if (d.frustums > 0) return
    const step = await page.evaluate(() => (window as unknown as { __perf: { governor: { step: number } } }).__perf.governor.step)
    if (step >= 2) {
      test.info().annotations.push({ type: 'governor', description: `step ${step}: frustums off before ${VEHICLE} was drawn` })
      return
    }
    if (Date.now() > deadline) throw new Error(`no frustum of the selected ${VEHICLE} (governor step ${step})`)
    await page.waitForTimeout(500)
  }
}

function noGpuErrors(w: Watch): void {
  expect(w.gl, 'WebGL errors and three warnings under the WebGL2 minimum limits').toEqual([])
  expect(w.errors, 'page errors').toEqual([])
}

test('demo build: synthcity entities never appear in shenzhen, suzhou or newyork, and come back on synthcity', async ({ page }) => {
  test.setTimeout(600_000)
  const base = baseUrl(DEMO_PORT)
  const w = await open(page, `${base}/world/${SHOW}?sel=${VEHICLE}`)
  const badge = page.locator('[data-demo-role]').first()
  await expect(badge).toHaveAttribute('data-surface', 'shared', { timeout: 60_000 })
  // the shared show of synthcity: vehicles, the deep-linked selection, field view, volumes, a target and its readout
  await expect(page.locator('[data-drone-id]')).toHaveCount(8, { timeout: 90_000 })
  await expect(page.locator(`[data-drone-id="${VEHICLE}"][data-selected]`)).toHaveCount(1, { timeout: 60_000 })
  await page.locator('[data-field-option="acoustic"]').first().click()
  await page.locator('[data-volumes-option="all"]').first().click()
  await expect(page.locator('[data-field-legend="acoustic"]')).toHaveCount(1)
  await expectFrustum(page)
  await page.locator('[data-entity="targets"]').first().click()
  await expect(page.locator('[data-target-row]')).toHaveCount(3, { timeout: 60_000 })
  await page.locator('[data-target-row]').first().click()
  await expect(page.locator('[data-target-detail]').first()).toBeVisible({ timeout: 30_000 })
  await page.locator('[data-target-tab="quant"]').first().click()
  await expect(page.locator('[data-quant-card]').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-crumb="run"]')).toHaveCount(1)
  await shot(page, 'd22w1-demo-synthcity')
  for (const city of CITIES) {
    await goWorld(page, city)
    // D2.2-W2: a recorded show, not static browsing (apps/web/src/app/shell/surface.ts resolveSurface)
    await expect(badge).toHaveAttribute('data-surface', 'shared', { timeout: 30_000 })
    await expect(badge).toHaveAttribute('data-city-replay', 'true', { timeout: 30_000 })
    await expect.poll(() => page.evaluate(() => (window as unknown as { __perf: { meta: { worldId: string } } }).__perf.meta.worldId), { timeout: 60_000 }).toBe(city)
    // eventually and then stably this city's own seven vehicles and seven targets (gen_city_watch.py), never synthcity's.
    // The rail tab is a UI preference, not reset by a world switch: round 0 leaves it on "targets" (below), so each
    // round switches back to "drones" before counting rows.
    for (let round = 0; round < 2; round++) {
      await page.locator('[data-entity="drones"]').first().click()
      await expect(page.locator('[data-drone-id]')).toHaveCount(N_CITY_VEHICLES, { timeout: 30_000 })
      await page.locator('[data-entity="targets"]').first().click()
      await expect(page.locator('[data-target-row]')).toHaveCount(N_CITY_TARGETS, { timeout: 30_000 })
      expect(new URL(page.url()).searchParams.has('sel'), 'sel of synthcity carried over across the world switch').toBe(false)
      expect((await perfDraws(page)).drones, `${city}: vehicles drawn`).toBeGreaterThan(0)
      if (round === 0) await page.waitForTimeout(4000)
    }
    await shot(page, `d22w1-demo-${city}`)
  }
  // back on the show: the show's entities are displayed again
  await goWorld(page, SHOW)
  await expect(badge).toHaveAttribute('data-surface', 'shared', { timeout: 30_000 })
  await page.locator('[data-entity="drones"]').first().click()
  await expect(page.locator('[data-drone-id]')).toHaveCount(8, { timeout: 90_000 })
  await page.locator(`[data-drone-id="${VEHICLE}"]`).first().click() // selects it and opens its detail page
  await expect.poll(() => new URL(page.url()).searchParams.get('sel'), { timeout: 30_000 }).toBe(VEHICLE)
  await expect(page.locator('[data-field-legend="acoustic"]')).toHaveCount(1, { timeout: 30_000 })
  await expect.poll(async () => (await perfDraws(page)).drones, { timeout: 60_000 }).toBeGreaterThan(0)
  await expect(page.locator('[data-crumb="run"]')).toHaveCount(1, { timeout: 30_000 })
  await shot(page, 'd22w1-demo-synthcity-back')
  noGpuErrors(w)
})

interface Iso {
  scope: { world: string | null; source: string | null; streamWorld: string | null; live: boolean; foreign: boolean; epoch: number }
  draws: Record<string, number>; staticBrowse: boolean; poses: number; frustums: number; roster: number; targets: number; targetBodies: number
  field: { profiles: number; volumes: boolean; quant: number; points: number; bodyAnchors: number; uavAnchors: number }
  tasking: { segments: number; nests: number; stations: number } | null; labels: number
  taskingStore: { nests: number; nestStatus: number; tasks: number }
}
/** the show's world-bound data the case expects on synthcity (vehicles, targets with their bodies, roster, nest status, task) */
const showFilled = (s: Iso): Record<string, boolean> => ({
  live: s.scope.live, poses: s.poses >= 8, targets: s.targets === 3, targetBodies: s.targetBodies > 0, roster: s.roster >= 8,
  nestStatus: s.taskingStore.nestStatus > 0, tasks: s.taskingStore.tasks > 0, fieldVolumes: s.field.volumes, fieldBodies: s.field.bodyAnchors > 0,
})
const ALL_FILLED = { live: true, poses: true, targets: true, targetBodies: true, roster: true, nestStatus: true, tasks: true, fieldVolumes: true, fieldBodies: true }
const iso = (page: Page): Promise<Iso> => page.evaluate(() => (window as unknown as { __vp: { isolation(): Iso } }).__vp.isolation())

test('test build: the data layer of every city holds no synthcity entity while the link stays on the show', async ({ page }) => {
  test.setTimeout(600_000)
  const base = baseUrl(TEST_PORT)
  const w = await open(page, `${base}/world/${SHOW}?sel=${VEHICLE}`)
  test.skip(!(await page.evaluate(() => '__vp' in window)), 'needs a test build')
  // the governor would switch frustums (and more) off on SwiftShader: hold it so the layers under test draw
  await page.evaluate(() => (window as unknown as { __vp: { governorHold(on: boolean): void } }).__vp.governorHold(true))
  await expect.poll(async () => (await iso(page)).scope.live, { timeout: 90_000 }).toBe(true)
  await page.locator('[data-field-option="acoustic"]').first().click()
  await page.locator('[data-volumes-option="all"]').first().click()
  await expect.poll(async () => showFilled(await iso(page)), { timeout: 120_000, message: 'synthcity show: vehicles, targets, bodies, roster, nests, task, field' })
    .toEqual(ALL_FILLED)
  await expect.poll(() => page.evaluate(() => (window as unknown as { __vp: { selected(): string[] } }).__vp.selected()), { timeout: 60_000 }).toEqual([VEHICLE])
  await expect.poll(async () => (await iso(page)).frustums, { timeout: 60_000, message: `frustum of the selected ${VEHICLE}` }).toBeGreaterThan(0)
  const before = await iso(page)
  test.info().annotations.push({ type: 'synthcity', description: JSON.stringify({ ...before, draws: undefined }) })
  for (const city of CITIES) {
    await goWorld(page, city)
    await expect.poll(async () => (await iso(page)).scope.world, { timeout: 60_000 }).toBe(city)
    // D2.2-W1, unchanged by D2.2-W2: the test build's `local` surface keeps a single live connection to the show
    // (synthcity) regardless of route, so this still proves the data layer rejects synthcity's data while the route
    // shows another city, foreign and empty — it does not exercise D2.2-W2's recorded replay (that is `shared`
    // surface only, the public demo build, covered by the "demo build" test above)
    for (let round = 0; round < 2; round++) {
      await expect.poll(async () => {
        const s = await iso(page)
        return { foreign: s.scope.foreign, live: s.scope.live, staticBrowse: s.staticBrowse, poses: s.poses, frustums: s.frustums, targets: s.targets,
          targetBodies: s.targetBodies, fieldPoints: s.field.points, bodyAnchors: s.field.bodyAnchors, uavAnchors: s.field.uavAnchors, volumes: s.field.volumes,
          profiles: s.field.profiles, quant: s.field.quant, tasking: s.tasking?.segments ?? 0, nests: s.tasking?.nests ?? 0, roster: s.roster,
          nestStatus: s.taskingStore.nestStatus, tasks: s.taskingStore.tasks,
          targetDraws: s.draws.targets ?? 0, taskingDraws: s.draws.tasking ?? 0, fieldDraws: s.draws.field ?? 0, frustumDraws: s.draws.frustums ?? 0 }
      }, { timeout: 30_000, message: `${city}: no synthcity entity in the data layer` }).toEqual({
        foreign: true, live: false, staticBrowse: true, poses: 0, frustums: 0, targets: 0, targetBodies: 0, fieldPoints: 0, bodyAnchors: 0, uavAnchors: 0,
        volumes: false, profiles: 0, quant: 0, tasking: 0, nests: 0, roster: 0, nestStatus: 0, tasks: 0, targetDraws: 0, taskingDraws: 0, fieldDraws: 0,
        frustumDraws: 0,
      })
      expect(new URL(page.url()).searchParams.has('sel')).toBe(false)
      expect(await page.evaluate(() => (window as unknown as { __vp: { selected(): string[] } }).__vp.selected())).toEqual([])
      if (round === 0) await page.waitForTimeout(4000)
    }
    await shot(page, `d22w1-test-${city}`)
  }
  await goWorld(page, SHOW)
  await expect.poll(async () => showFilled(await iso(page)), { timeout: 120_000, message: 'back on synthcity: the show fills the data layer again' })
    .toEqual(ALL_FILLED)
  await page.evaluate((id) => (window as unknown as { __vp: { select(ids: string[]): void } }).__vp.select([id]), VEHICLE)
  await expect.poll(async () => (await iso(page)).frustums, { timeout: 60_000, message: `frustum of ${VEHICLE} on synthcity again` }).toBeGreaterThan(0)
  await shot(page, 'd22w1-test-synthcity-back')
  noGpuErrors(w)
})

/** every city with a recorded show (D2.2-W2 CITY_SHOW_FIXTURE): all six, at 1920x1080, with the fleet, the targets and
 *  the tasking lines on screen (AWR_SHOTS_DIR/<city>.png) */
const ALL_SIX_CITIES = ['shenzhen', 'suzhou', 'newyork', 'chicago', 'shanghai', 'sanfrancisco'] as const

test('demo build: 1920x1080 screenshot of every city\'s own recorded show (fleet, targets, tasking)', async ({ page }) => {
  test.setTimeout(600_000)
  await page.setViewportSize({ width: 1920, height: 1080 })
  const base = baseUrl(DEMO_PORT)
  const w = await open(page, `${base}/world/${ALL_SIX_CITIES[0]}`)
  const badge = page.locator('[data-demo-role]').first()
  for (const city of ALL_SIX_CITIES) {
    await goWorld(page, city)
    await expect(badge).toHaveAttribute('data-surface', 'shared', { timeout: 60_000 })
    await expect(badge).toHaveAttribute('data-city-replay', 'true', { timeout: 30_000 })
    // the rail tab is a UI preference: force it back to "drones" (the previous city's loop iteration leaves it on
    // "targets") before counting rows
    await page.locator('[data-entity="drones"]').first().click()
    await expect(page.locator('[data-drone-id]')).toHaveCount(N_CITY_VEHICLES, { timeout: 60_000 })
    await page.locator('[data-entity="targets"]').first().click()
    await expect(page.locator('[data-target-row]')).toHaveCount(N_CITY_TARGETS, { timeout: 30_000 })
    await expect.poll(async () => (await perfDraws(page)).drones, { timeout: 30_000, message: `${city}: vehicles drawn` }).toBeGreaterThan(0)
    await page.waitForTimeout(800) // a settled frame with the fleet, targets and tasking lines visible before the shot
    await shot(page, city)
  }
  noGpuErrors(w)
})
