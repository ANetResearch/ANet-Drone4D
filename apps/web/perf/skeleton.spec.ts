// D1-AC-34 walking skeleton (AWR-03 §8.4, §8.6 D1-MS3), the integration gate: one-city World -> Range -> point cloud on
// screen -> 1 FleetSim vehicle -> StateRing -> Gateway -> WS -> drone on screen -> take-off -> ground pick -> goto
// accepted, running, succeeded with the vehicle within 3 m of the point above the pick, no pageerror.
//
// Data sources:
//   1. full chain (the gate): the real backend, api + sim-core under the supervisor. SKELETON_API=host:port uses a
//      running `make run`; otherwise the spec starts `python -m awr.runtime.supervisor --profile ci --only sim-core,api`
//      on free ports (perf/skeleton.server.ts startBackend). The browser talks to the api directly: the SPA from
//      apps/web/dist, /worlds with Range, REST and the /api/rt WebSocket; nothing is proxied or stubbed.
//      The same backend also checks the D1-AC-01 clause "/world/<id> accessible" for the six cities.
//   2. FakeSource (?source=fake, D1-AC-35 frontend part): the browser-side gateway stand-in flies the goto, served by
//      perf/skeleton.server.ts (dist, /worlds with Range, ray_hit over the DSM); runs without any backend;
//   3. awr.rt.v1 over a real WebSocket to tools/fake/fake_gw.py (D1-AC-35 protocol path; fake_gw only acknowledges).
// Build first: `VITE_AWR_TEST_SWITCHES=1 make build` (the spec uses the window.__vp and __ux test hooks).
// Screenshots: /data/projs/anet-drone/.cache/impl/shots/skeleton-*.png.
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { freePort, startBackend, startSkeletonServer, type Backend, type SkeletonServer } from './skeleton.server'

const ROOT = resolve(import.meta.dirname, '../../..')
const SHOTS = resolve(ROOT, '.cache/impl/shots')
// the static server and fake_gw take free ports: the former fixed 4193 + 10k and 8097 + 10k collided with the vite preview
// and api of neighbouring offsets when several worktrees run at once (ADR-055; FX-GW)
/** fixed principal hint (16-64 base32): reruns against one `make run` keep the operator seat (AWR-12 §4.2.2 T01) */
const PRINCIPAL_HINT = 'SKELETONEEPLAYWRIGHTGATE'
const CITIES = ['shenzhen', 'shanghai', 'suzhou', 'chicago', 'newyork', 'sanfrancisco']

function testBuild(): boolean {
  // the same build the skeleton server and the backend serve (AWR_PERF_DIST or M11_DIST: a private test build; FX-GW)
  const dir = resolve(process.env.AWR_PERF_DIST ?? process.env.M11_DIST ?? resolve(import.meta.dirname, '../dist'), 'assets')
  if (!existsSync(dir)) return false
  return readdirSync(dir).some((f) => f.endsWith('.js') && readFileSync(resolve(dir, f), 'utf8').includes('__vp'))
}

type Cmd = 'takeoff' | 'hover' | 'land' | 'rtl'
type Vp = {
  project(e: number, n: number, u: number): [number, number] | null
  ground(): { surface: number[]; target: number[] | null } | null
  clearPick(): void
  gotoState(): string | null
  gotoResults(): string[]
  cmdState(op: Cmd): string | null
  cmdResults(op: Cmd): string[]
  dronePose(id: string): [number, number, number] | null
  flightState(id: string): string | null
  conn(): string
  roster(): { id: string }[]
  session(): { role: string; seat: string; runId: string; worldId: string } | null
  backend(): { tier: string; deviceClass: string } | null
}
/** run fn(window.__vp) in the page (fn is serialised; it must not close over test variables) */
const vp = <T>(page: Page, fn: (v: Vp) => T): Promise<T> => page.evaluate(`(${fn.toString()})(window.__vp)`) as Promise<T>
const waitVp = (page: Page, fn: (v: Vp) => boolean, timeout: number): Promise<unknown> =>
  page.waitForFunction(`(${fn.toString()})(window.__vp)`, null, { timeout })

async function boot(page: Page, url: string, errors: string[]): Promise<void> {
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto(url)
  await page.waitForFunction(() => {
    const w = window as unknown as { __ux?: { boot?: { state?: string } }; __vp?: { conn(): string }; __perf?: { load?: { ttfp?: number } } }
    return w.__ux?.boot?.state === 'REVEALED' && w.__vp?.conn() === 'LIVE' && (w.__perf?.load?.ttfp ?? 0) > 0
  }, null, { timeout: 90_000 })
}

/** Tier S classic renderer, first screen from one Range, points drawn within the fixed budget (AWR-16 §4.11) */
async function pointCloudChecks(page: Page): Promise<void> {
  expect(await vp(page, (v) => v.backend())).toMatchObject({ tier: 'S', deviceClass: 'software' })
  const perf = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { load: { ttfp: number; firstScreenBytes: number }; pc: { drawn: number }; gpu: { calls: number; passPlan: number } } }).__perf
    return { ttfp: p.load.ttfp, bytes: p.load.firstScreenBytes, drawn: p.pc.drawn, calls: p.gpu.calls, plan: p.gpu.passPlan }
  })
  expect(perf.bytes).toBe(321_384) // Shenzhen Tier S first screen (AWR-16 §4.11)
  expect(perf.drawn).toBeGreaterThan(10_000)
  expect(perf.drawn).toBeLessThanOrEqual(25_000)
  expect(perf.calls).toBe(perf.plan) // one renderer.render, draw calls equal the plan (AWR-03 §3.6 rule 2)
}

/** DSM surface z (world ENU m) at (e, n) through R07 `height_dsm` with a viewer token (AWR-17 §4.3.2) */
async function surfaceZ(base: string, e: number, n: number): Promise<number> {
  const tok = (await (await fetch(`${base}/api/auth/token`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ role: 'viewer' }) })).json()) as { token: string }
  const r = await fetch(`${base}/api/world/shenzhen/query`, {
    method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${tok.token}` },
    body: JSON.stringify({ op: 'height_dsm', points: [[e, n]] }),
  })
  expect(r.status, 'R07 height_dsm').toBe(200)
  return ((await r.json()) as { z_m: number[] }).z_m[0]
}

/** select the vehicle in the DroneRail, click the ground at an ENU point, press GoTo; returns the goto target */
async function gotoByUi(page: Page, id: string, groundEnu: [number, number]): Promise<number[]> {
  await page.locator(`[data-drone-id="${id}"]`).click()
  const scr = await page.evaluate(([e, n]) => (window as unknown as { __vp: Vp }).__vp.project(e, n, 0), groundEnu)
  expect(scr, 'ground point on screen').not.toBeNull()
  await page.mouse.click(scr![0], scr![1])
  await waitVp(page, (v) => v.ground()?.target != null, 15_000)
  const target = (await vp(page, (v) => v.ground()!.target))!
  const btn = page.getByRole('button', { name: '飞到此处' })
  await expect(btn).toBeEnabled()
  await btn.click()
  return target
}

test.describe.configure({ mode: 'serial' })

// ------------------------------------------------------------ 1. the gate: real backend (api + sim-core)
test.describe('walking skeleton full chain (D1-AC-34)', () => {
  let be: Backend
  test.skip(!testBuild(), 'needs a VITE_AWR_TEST_SWITCHES=1 build in apps/web/dist')
  test.beforeAll(async () => {
    test.setTimeout(180_000)
    mkdirSync(SHOTS, { recursive: true })
    be = await startBackend(undefined, { AWR_SCENARIO_LOAD: '0' })   // skeleton: 1 vehicle, no scenario (M10 loads S1 by default)
  })
  test.afterAll(async () => {
    await be?.close()
  })

  test('World -> Range -> point cloud -> FleetSim -> StateRing -> Gateway -> WS -> drone -> takeoff -> ground pick -> goto', async ({ browser }) => {
    test.setTimeout(420_000)
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 } })
    await ctx.addInitScript((hint) => {
      try {
        localStorage.setItem('awr.principal_hint', hint)
      } catch {
        // storage unavailable: the server issues a fresh principal
      }
    }, PRINCIPAL_HINT)
    const page = await ctx.newPage()
    const errors: string[] = []
    const httpErrors: string[] = []
    let ranges = 0
    page.on('response', (r) => {
      const u = new URL(r.url())
      if (r.status() >= 400) httpErrors.push(`${r.status()} ${r.request().method()} ${u.pathname}`)
      if (r.status() === 206 && u.pathname.startsWith('/worlds/shenzhen/')) ranges++
    })
    try {
      await boot(page, `${be.url}/world/shenzhen`, errors)
      // one first-screen Range, served by the api (AWR-17 §5.3), cross-origin isolated (COOP/COEP, §5.4)
      expect(ranges).toBe(1)
      expect(await page.evaluate(() => crossOriginIsolated)).toBe(true)
      await pointCloudChecks(page)
      // the session holds the operator seat (POST /api/auth/token -> seat_claim), world and run of the backend
      const session = (await vp(page, (v) => v.session()))!
      expect(session, 'operator seat (another principal holds it: close its tabs and wait the 30 s GRACE)').toMatchObject({ role: 'operator', seat: 'held', worldId: 'shenzhen' })
      // FleetSim -> StateRing -> Gateway -> WS: the roster has the one vehicle and the drone layer has its pose
      await waitVp(page, (v) => v.roster().length > 0, 30_000)
      const id = (await vp(page, (v) => v.roster()[0].id))!
      expect(await vp(page, (v) => v.roster().length)).toBe(1)
      await waitVp(page, (v) => v.roster().length > 0 && v.dronePose(v.roster()[0].id) !== null, 15_000)
      await page.waitForFunction(() => (window as unknown as { __perf: { net: { swarmHz: number } } }).__perf.net.swarmHz > 5, null, { timeout: 15_000 })
      await expect(page.locator('[data-rail-row]')).toHaveCount(1)
      const pose0 = (await page.evaluate((i) => (window as unknown as { __vp: Vp }).__vp.dronePose(i), id))!
      await page.screenshot({ path: resolve(SHOTS, 'skeleton-01-world.png') })

      // take-off from the detail page (AlertDialog, default 2.5 m AGL): the vehicle must fly before GoTo (g04 §6.2);
      // a rerun against the same `make run` finds it already FLYING and goes straight to GoTo
      await page.locator(`[data-drone-id="${id}"]`).click()
      await waitVp(page, (v) => v.roster().length > 0 && v.flightState(v.roster()[0].id) !== null, 15_000)
      const fs0 = await page.evaluate((i) => (window as unknown as { __vp: Vp }).__vp.flightState(i), id)
      const ground0 = await surfaceZ(be.url, pose0[0], pose0[1]) // DSM under the vehicle (R07 height_dsm)
      if (fs0 !== 'FLYING') {
        await page.locator('[data-cmd="takeoff"]').click()
        await expect(page.locator('[data-cmd-confirm="takeoff"]')).toBeVisible()
        await expect(page.locator('[data-cmd-confirm="takeoff"]')).toHaveText('起飞')
        await page.waitForTimeout(600) // dialog enter transition
        await page.screenshot({ path: resolve(SHOTS, 'skeleton-02a-takeoff-dialog.png') })
        await page.locator('[data-cmd-confirm="takeoff"]').click()
        await waitVp(page, (v) => v.cmdState('takeoff') === 'succeeded' || ['rejected', 'failed', 'timeout'].includes(v.cmdState('takeoff') ?? ''), 60_000)
        expect(await vp(page, (v) => v.cmdResults('takeoff'))).toEqual(['accepted', 'running', 'succeeded'])
        await page.waitForTimeout(800) // interpolation delay: the rendered pose settles
        const pose1 = (await page.evaluate((i) => (window as unknown as { __vp: Vp }).__vp.dronePose(i), id))!
        expect(pose1[2] - pose0[2]).toBeGreaterThan(1.5)
        await page.screenshot({ path: resolve(SHOTS, 'skeleton-02-takeoff.png') })
      }

      // click open ground near the vehicle: candidate ENU points around it, keep the first whose ray_hit lands on open
      // ground (surface within 3 m of the aimed point, <= 2 m above the take-off ground) inside the free canvas area
      const view = page.viewportSize()!
      const RAIL_W = 300 // right DroneRail
      let target: number[] | null = null
      let surface: number[] | null = null
      const offsets: [number, number][] = [[-40, -30], [40, -30], [-40, 30], [40, 30], [0, -45], [-50, 0], [50, 0], [0, 45], [-25, -25], [25, -25]]
      for (const [de, dn] of offsets) {
        const aim: [number, number, number] = [pose0[0] + de, pose0[1] + dn, ground0]
        const scr = await page.evaluate(([e, n, u]) => (window as unknown as { __vp: Vp }).__vp.project(e, n, u), aim)
        if (!scr || scr[0] < 300 || scr[0] > view.width - RAIL_W - 40 || scr[1] < 110 || scr[1] > view.height - 120) continue
        await vp(page, (v) => v.clearPick())
        await page.mouse.click(scr[0], scr[1])
        try {
          await waitVp(page, (v) => v.ground()?.target != null, 10_000)
        } catch {
          continue
        }
        const g = (await vp(page, (v) => v.ground()))!
        if (Math.hypot(g.surface[0] - aim[0], g.surface[1] - aim[1]) > 3 || g.surface[2] - ground0 > 2) continue
        target = g.target
        surface = g.surface
        break
      }
      expect(target, 'an open ground point near the vehicle is clickable').not.toBeNull()
      // above the pick: same E/N, z = max(current z, hit + 10 m) (AWR-14 §6.7)
      expect(target![0]).toBeCloseTo(surface![0], 3)
      expect(target![1]).toBeCloseTo(surface![1], 3)
      expect(target![2]).toBeGreaterThanOrEqual(surface![2] + 10 - 1e-3)
      const btn = page.getByRole('button', { name: '飞到此处' })
      await expect(btn).toBeEnabled()
      await btn.click()
      await waitVp(page, (v) => v.gotoState() === 'running' || v.gotoState() === 'failed', 15_000)
      expect(await vp(page, (v) => v.gotoState())).toBe('running')
      await page.screenshot({ path: resolve(SHOTS, 'skeleton-03-goto-running.png') })
      await waitVp(page, (v) => v.gotoState() === 'succeeded' || v.gotoState() === 'failed', 180_000)
      expect(await vp(page, (v) => v.gotoResults())).toEqual(['accepted', 'running', 'succeeded'])
      await page.waitForTimeout(1000) // interpolation delay: the rendered pose settles
      const pose = (await page.evaluate((i) => (window as unknown as { __vp: Vp }).__vp.dronePose(i), id))!
      const err = Math.hypot(pose[0] - target![0], pose[1] - target![1], pose[2] - target![2])
      expect(err, `vehicle ${pose.join(',')} vs target ${target!.join(',')}`).toBeLessThanOrEqual(3)
      await page.screenshot({ path: resolve(SHOTS, 'skeleton-04-arrived.png') })
      // close-up: orbit camera focus on the selected vehicle (F), the drone and the GoTo marker at 60 m
      await page.getByRole('button', { name: '聚焦选中' }).first().click() // camera toolbar and detail panel both offer it
      await page.waitForTimeout(2000)
      await page.screenshot({ path: resolve(SHOTS, 'skeleton-05-focus.png') })
      expect(errors).toEqual([])
      expect(httpErrors).toEqual([])
    } finally {
      await ctx.close()
    }
  })

  test('D1-AC-01: /world/<id> is served and opens for the six cities', async ({ browser }) => {
    test.setTimeout(420_000)
    for (const city of CITIES) {
      const r = await fetch(`${be.url}/world/${city}`)
      expect(r.status, `/world/${city}`).toBe(200)
      expect(r.headers.get('content-type') ?? '').toContain('text/html')
      const wj = await fetch(`${be.url}/worlds/${city}/world.json`)
      expect(wj.status, `/worlds/${city}/world.json`).toBe(200)
      expect(((await wj.json()) as { id: string }).id).toBe(city)
      const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })
      const errors: string[] = []
      page.on('pageerror', (e) => errors.push(e.message))
      try {
        await page.goto(`${be.url}/world/${city}`)
        await page.waitForFunction(() => {
          const w = window as unknown as { __ux?: { boot?: { state?: string } }; __perf?: { load?: { ttfp?: number }; pc?: { drawn?: number } } }
          return w.__ux?.boot?.state === 'REVEALED' && (w.__perf?.load?.ttfp ?? 0) > 0 && (w.__perf?.pc?.drawn ?? 0) > 0
        }, null, { timeout: 90_000 })
        if (city !== 'shenzhen') await page.screenshot({ path: resolve(SHOTS, `skeleton-city-${city}.png`) })
        expect(errors, city).toEqual([])
      } finally {
        await page.close()
      }
    }
  })
})

// ------------------------------------------------------------ 2, 3. early data sources (D1-AC-35 frontend part)
test.describe('walking skeleton early data sources (D1-AC-35)', () => {
  let srv: SkeletonServer
  test.skip(!testBuild(), 'needs a VITE_AWR_TEST_SWITCHES=1 build in apps/web/dist')
  test.beforeAll(async () => {
    mkdirSync(SHOTS, { recursive: true })
    srv = await startSkeletonServer(0)
  })
  test.afterAll(async () => {
    await srv?.close()
  })

  test('FakeSource: point cloud, one drone, ray_hit, goto closes', async ({ page }) => {
    test.setTimeout(240_000)
    const errors: string[] = []
    await boot(page, `${srv.url}/world/shenzhen?source=fake&fakeN=1&fakeStart=-100,-150,60`, errors)
    await pointCloudChecks(page)
    expect(srv.stats.ranges).toBe(1)
    // DroneRail shows the fleet summary row; the drone layer has the vehicle at its pose
    await expect(page.locator('[data-rail-row]')).toHaveCount(1)
    expect(await vp(page, (v) => v.dronePose('p600-01'))).toEqual([-100, -150, 60])
    await page.screenshot({ path: resolve(SHOTS, 'skeleton-fake-01-world.png') })
    // click the ground, GoTo, fly
    const target = await gotoByUi(page, 'p600-01', [-40, -170])
    expect(srv.stats.rayHits).toBeGreaterThanOrEqual(1)
    expect(target[2]).toBeGreaterThanOrEqual(60) // keep the current altitude, >= hit + 10 m (AWR-14 §6.7)
    await waitVp(page, (v) => v.gotoState() === 'running', 15_000)
    await page.screenshot({ path: resolve(SHOTS, 'skeleton-fake-02-goto-running.png') })
    await waitVp(page, (v) => v.gotoState() === 'succeeded', 60_000)
    expect(await vp(page, (v) => v.gotoResults())).toEqual(['accepted', 'running', 'succeeded'])
    await page.waitForTimeout(800) // interpolation delay: the rendered pose settles
    const pose = (await vp(page, (v) => v.dronePose('p600-01')))!
    const err = Math.hypot(pose[0] - target[0], pose[1] - target[1], pose[2] - target[2])
    expect(err, `vehicle ${pose.join(',')} vs target ${target.join(',')}`).toBeLessThanOrEqual(3)
    await page.screenshot({ path: resolve(SHOTS, 'skeleton-fake-03-arrived.png') })
    expect(errors).toEqual([])
  })

  test('awr.rt.v1 over WebSocket to fake_gw: handshake, subscriptions, roster, call', async ({ browser }) => {
    test.setTimeout(240_000)
    const py = resolve(ROOT, '.venv/bin/python')
    test.skip(!existsSync(py), 'Python venv with tools/fake/fake_gw.py dependencies not found')
    const GW_PORT = await freePort()
    const gw: ChildProcess = spawn(py, ['tools/fake/fake_gw.py', '--n', '1', '--port', String(GW_PORT), '--no-detail', '--calls', 'ack'], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] })
    try {
      await new Promise<void>((ok, fail) => {
        const t = setTimeout(() => fail(new Error('fake_gw did not start')), 30_000)
        gw.stdout!.on('data', (d: Buffer) => {
          if (d.toString().includes('READY')) {
            clearTimeout(t)
            ok()
          }
        })
      })
      const proxied = await startSkeletonServer(0, { apiProxy: { host: '127.0.0.1', port: GW_PORT } })
      const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })
      const errors: string[] = []
      try {
        await boot(page, `${proxied.url}/world/shenzhen`, errors)
        expect(await vp(page, (v) => v.roster().map((r) => r.id))).toEqual(['uav0001'])
        await waitVp(page, (v) => v.dronePose('uav0001') !== null, 15_000)
        // rolling 1 s rate window in rt.worker
        await page.waitForFunction(() => (window as unknown as { __perf: { net: { swarmHz: number } } }).__perf.net.swarmHz > 5, null, { timeout: 10_000 })
        const net = await page.evaluate(() => {
          const n = (window as unknown as { __perf: { net: { swarmHz: number; rttMs: number; epoch: number } } }).__perf.net
          return { swarmHz: n.swarmHz, rtt: n.rttMs, epoch: n.epoch }
        })
        expect(net.swarmHz).toBeGreaterThan(5) // Tier S default swarm subscription is 10 Hz
        expect(net.rtt).toBeGreaterThanOrEqual(0)
        await gotoByUi(page, 'uav0001', [-40, -170])
        await waitVp(page, (v) => v.gotoState() === 'succeeded', 30_000)
        await page.screenshot({ path: resolve(SHOTS, 'skeleton-fake-gw.png') })
        expect(errors).toEqual([])
      } finally {
        await page.close()
        await proxied.close()
      }
    } finally {
      gw.kill('SIGTERM')
    }
  })
})
