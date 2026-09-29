// M06 viewport smoke (functional, not a performance case): the test build boots on SwiftShader C1 (Tier S), the shader
// zoo runs under the mask, the render phase matches the pass plan every frame (M06-AC-016), FakeSource vehicles reach
// the drone layer (markers, glyphs), camera modes switch through the facade, labels and the ViewCube exist, and no
// page error or M06 diagnostic error is logged. Needs a test build (VITE_AWR_TEST_SWITCHES=1) in M06_DIST or dist/.
import { expect, test } from '@playwright/test'
import { openWorld, watch } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

test('viewport boots, warms up and renders exactly the pass plan (Tier S)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv!.url)
  const info = await page.evaluate(() => (window as unknown as { __vp: { backendInfo(): unknown } }).__vp.backendInfo())
  expect(info).toMatchObject({ tier: 'S', deviceClass: 'software', state: 'READY', pointSizeMode: 'glpoint' })
  // pass plan equality on consecutive frames
  const samples = await page.evaluate(async () => {
    const p = (window as unknown as { __perf: { gpu: { calls: number; passPlan: number; planMismatches: number } } }).__perf
    const out: [number, number][] = []
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => requestAnimationFrame(() => r(null)))
      out.push([p.gpu.calls, p.gpu.passPlan])
    }
    return { out, mismatches: p.gpu.planMismatches }
  })
  for (const [c, plan] of samples.out) expect(c).toBe(plan)
  expect(samples.mismatches).toBe(0)
  // FakeSource vehicles reach the drone layer
  await page.waitForFunction(() => ((window as unknown as { __vp: { drones(): { n: number } | null } }).__vp.drones()?.n ?? 0) >= 5, null, { timeout: 30_000 })
  const d = await page.evaluate(() => (window as unknown as { __vp: { drones(): unknown; specs(): unknown } }).__vp.drones())
  expect(d).toMatchObject({ n: 5 })
  const specs = await page.evaluate(() => (window as unknown as { __vp: { specs(): { id: string }[] } }).__vp.specs().map((s) => s.id).sort())
  for (const id of ['drones', 'trails', 'glyphs', 'frustums', 'mission', 'zones', 'groundSky', 'labels', 'debug', 'pointcloud']) expect(specs).toContain(id)
  await expect(page.locator('[data-viewcube] [role="button"]')).toHaveCount(54)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})

test('camera modes through the facade; third/fpv guards', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv!.url)
  type VP = { __vp: { setMode(m: string): { ok: boolean; reason?: string }; camera(): { mode: string } | null; select(ids: string[]): void; roster(): { id: string }[] } }
  const r0 = await page.evaluate(() => (window as unknown as VP).__vp.setMode('third'))
  expect(r0).toEqual({ ok: false, reason: 'no_focus' })
  for (const m of ['bird', 'free', 'orbit']) {
    const r = await page.evaluate((mm) => (window as unknown as VP).__vp.setMode(mm), m)
    expect(r.ok).toBe(true)
    await page.waitForTimeout(300)
    expect(await page.evaluate(() => (window as unknown as VP).__vp.camera()?.mode)).toBe(m)
  }
  await page.waitForFunction(() => (window as unknown as VP).__vp.roster().length >= 1, null, { timeout: 30_000 })
  const id = await page.evaluate(() => (window as unknown as VP).__vp.roster()[0].id)
  await page.evaluate((i) => (window as unknown as VP).__vp.select([i]), id)
  const r3 = await page.evaluate(() => (window as unknown as VP).__vp.setMode('third'))
  expect(r3.ok).toBe(true)
  await page.waitForTimeout(1500)
  const fpv = await page.evaluate(() => (window as unknown as VP).__vp.setMode('fpv'))
  // FPV needs M13 sensors: without them the guard answers no_camera_sensor
  expect(fpv.ok || fpv.reason === 'no_camera_sensor').toBe(true)
  await page.evaluate(() => (window as unknown as VP).__vp.setMode('orbit'))
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})

test('context loss rebuilds the viewport and resumes rendering (M06-AC-009, FR-010)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv!.url)
  type P = { __perf: { gpu: { contextLost: number; calls: number; passPlan: number }; frame: { count: number } } }
  type V = { __vp: { loseContext(): boolean; backendInfo(): { state: string; programs: number } | null } }
  const lostAt = await page.evaluate(() => {
    const ok = (window as unknown as V).__vp.loseContext()
    return ok ? performance.now() : -1
  })
  expect(lostAt).toBeGreaterThan(0)
  await page.waitForFunction(() => (window as unknown as P).__perf.gpu.contextLost >= 1, null, { timeout: 10_000 })
  const f0 = await page.evaluate(() => (window as unknown as P).__perf.frame.count)
  // a new canvas, a new backend, warm-up, READY and frames again
  await page.waitForFunction((n) => {
    const i = (window as unknown as V).__vp.backendInfo()
    return i?.state === 'READY' && (window as unknown as P).__perf.frame.count > n + 10
  }, f0, { timeout: 30_000 })
  const recoverMs = await page.evaluate((t) => performance.now() - t, lostAt)
  test.info().annotations.push({ type: 'recovery', description: `${recoverMs.toFixed(0)} ms` })
  if (process.env.M06_PERF === '1') expect(recoverMs).toBeLessThanOrEqual(3000)
  const g = await page.evaluate(() => (window as unknown as P).__perf.gpu)
  expect(g.calls).toBe(g.passPlan)
  expect(w.errors).toEqual([])
  expect(w.m06.filter((m) => !/M06-E002/.test(m))).toEqual([])
})
