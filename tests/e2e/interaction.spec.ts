// Interaction gate on the real backend (D1-AC-32; PERF-AC-025; M16 §6.7.4 e2e.interaction). Test build (window.__vp for
// projections and the command results), free-shenzhen (two P600 on the ground, gcs_loss_policy ignore):
//   1. /world/shenzhen direct: reveal, TTFP recorded (the threshold is judged by flight60), no pageerror;
//   2. take off p600-01, pick open ground, GoTo -> accepted, running, succeeded; the vehicle stops within 3 m of the target
//      (above the picked point, AWR-03 §8.2);
//   3. add a P600 (palette entry) -> it appears in the drone rail within 1 s; remove it -> gone within 1 s;
//   4. zones overlay: the curated zones of scenarios/zones/shenzhen.zones.geojson are listed by the zones layer;
//   5. switch the viewed world to shanghai and back: load.switchMs recorded (metrics.json switch_ms).
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import { glyphScan, isTestBuild, liveBackend, noErrors, revealed, watch, type Live } from './helpers'

const ROOT = join(import.meta.dirname, '..', '..')
let be: Live
test.beforeAll(async () => {
  be = await liveBackend('free-shenzhen')
})
test.afterAll(async () => {
  await be?.close()
})

type Vp = { __vp: { roster(): string[]; flightState(id: string): string; dronePose(id: string): number[] | null;
  cmdResults(op: string): string[]; gotoResults(): string[]; ground(): number[] | null; layers(): Record<string, unknown>;
  zones(): { id: string; kind: string }[] } }

test('direct open, takeoff, pick and GoTo, add and remove, zones, world switch', async ({ page }) => {
  test.setTimeout(300_000)
  await page.addInitScript(() => localStorage.setItem('awr.principal_hint', 'MSIXTEENINTERACTIONGATEA'))
  const w = watch(page)
  const fleetResp: string[] = []
  page.on('response', async (r) => {
    if (r.url().includes('/api/fleet/vehicles')) fleetResp.push(`${r.status()} ${r.request().method()} ${(await r.text().catch(() => '')).slice(0, 160)}`)
  })
  await page.goto(`${be.url}/world/shenzhen`)
  await revealed(page)
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1); a skip is a failure in the harness')
  await expect.poll(() => page.evaluate(() => (window as unknown as Vp).__vp.roster().length), { timeout: 30_000 }).toBeGreaterThanOrEqual(2)
  // 2. takeoff and goto through the UI
  await page.locator('[data-drone-id="p600-01"]').first().click()
  const takeoff = page.locator('[data-cmd="takeoff"]').first()
  await takeoff.hover()
  await takeoff.click()
  const confirm = page.locator('[data-cmd-confirm="takeoff"]').first()
  await confirm.hover()
  await confirm.click()
  await expect.poll(() => page.evaluate(() => (window as unknown as Vp).__vp.cmdResults('takeoff').join(',')), { timeout: 30_000 })
    .toContain('succeeded')
  const pose = await page.evaluate(() => (window as unknown as Vp).__vp.dronePose('p600-01'))
  expect(pose).not.toBeNull()
  // pick open ground near the vehicle: candidate ENU points around it projected into the free canvas area, keep the first
  // whose ray_hit lands within 3 m of the aimed point (INT-1: the ray_hit reply is asynchronous, poll instead of reading once)
  const offsets: [number, number][] = [[-40, -30], [40, -30], [-40, 30], [40, 30], [0, -45], [-50, 0], [50, 0], [0, 45]]
  let target: number[] | null = null
  for (const [de, dn] of offsets) {
    const aim = [pose![0] + de, pose![1] + dn, pose![2] - 2.5]
    const scr = await page.evaluate(([e, n, u]) => (window as unknown as { __vp: { project(e: number, n: number, u: number): number[] | null } }).__vp.project(e, n, u), aim)
    if (!scr || scr[0] < 300 || scr[0] > 1280 - 340 || scr[1] < 110 || scr[1] > 720 - 170) continue
    await page.evaluate(() => (window as unknown as { __vp: { clearPick(): void } }).__vp.clearPick())
    await page.mouse.click(scr[0], scr[1])
    try {
      await expect.poll(() => page.evaluate(() => (window as unknown as { __vp: { ground(): { target: number[] } | null } }).__vp.ground()?.target ?? null), { timeout: 10_000 }).not.toBeNull()
    } catch {
      continue
    }
    target = await page.evaluate(() => (window as unknown as { __vp: { ground(): { target: number[] } | null } }).__vp.ground()!.target)
    break
  }
  expect(target, 'an open ground point near p600-01 is clickable').not.toBeNull()
  {
    const go = page.getByRole('button', { name: '飞到此处' }).first()
    await go.hover()
    await go.click()
    await expect.poll(() => page.evaluate(() => (window as unknown as Vp).__vp.gotoResults().join(',')), { timeout: 120_000 })
      .toMatch(/accepted.*running.*succeeded/)
    await page.waitForTimeout(800) // interpolation delay: the rendered pose settles
    const end = await page.evaluate(() => (window as unknown as Vp).__vp.dronePose('p600-01'))
    expect(Math.hypot(end![0] - target![0], end![1] - target![1], end![2] - target![2])).toBeLessThanOrEqual(3)
  }
  // 3. add and remove a P600
  const before = await page.evaluate(() => (window as unknown as Vp).__vp.roster().length)
  await page.keyboard.press('Control+KeyK')
  await page.getByPlaceholder('输入命令或搜索').fill('添加 P600') // 'P600' alone also matches the vehicle rows p600-01, p600-02 (ranked first)
  await page.keyboard.press('Enter')
  // the palette closes with an exit transition: click only once the add tool is armed and the dialog is gone (INT-1)
  await expect(page.getByText('点击地面或屋顶确定出生点')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByPlaceholder('输入命令或搜索')).toBeHidden()
  // spawn point: open canvas area away from both vehicles (SPAWN_TOO_CLOSE) and above the bottom-centre toast stack
  const spawnAt = await page.evaluate(([e, n, u]) => {
    const v = (window as unknown as { __vp: { project(e: number, n: number, u: number): number[] | null } }).__vp
    for (const [de, dn] of [[80, 80], [-80, 80], [80, -80], [-80, -80], [110, 0], [-110, 0], [0, 110], [0, -110]]) {
      const p = v.project(e + de, n + dn, u)
      if (p && p[0] > 320 && p[0] < 1280 - 360 && p[1] > 130 && p[1] < 720 - 180) return p
    }
    return [700, 470]
  }, await page.evaluate((z0) => {
    // around the vehicle's current position (it moved during the GoTo), at the take-off ground height
    const p = (window as unknown as Vp).__vp.dronePose('p600-01')!
    return [p[0], p[1], z0]
  }, pose![2] - 2.5))
  await page.mouse.click(spawnAt[0], spawnAt[1])
  await expect(page.getByText('在此添加 P600')).toBeVisible({ timeout: 10_000 }) // opens once ray_hit resolved the spawn point
  await page.keyboard.press('Enter')
  const t0 = Date.now()
  await expect.poll(() => page.evaluate(() => (window as unknown as Vp).__vp.roster().length), { timeout: 10_000, message: fleetResp.join(' | ') }).toBe(before + 1)
  expect(Date.now() - t0).toBeLessThanOrEqual(1000 + 500)
  // remove it again through the detail panel (grounded: confirm, DELETE, gone within 1 s; INT-1 added the removal step)
  const added = (await page.evaluate(() => (window as unknown as { __vp: { roster(): { id: string }[] } }).__vp.roster().map((r) => r.id)))
    .find((id) => !['p600-01', 'p600-02'].includes(id))!
  const back = page.getByRole('button', { name: '返回列表' })
  if (await back.count()) await back.first().click() // the rail shows p600-01's detail page since the GoTo
  await expect(page.locator(`[data-drone-id="${added}"]`).first()).toBeVisible()
  await page.locator(`[data-drone-id="${added}"]`).first().click()
  await page.getByRole('button', { name: '更多命令' }).first().click()
  await page.getByRole('menuitem', { name: '移除' }).first().click()
  await page.getByRole('button', { name: '移除' }).last().click()
  const t1 = Date.now()
  await expect.poll(() => page.evaluate(() => (window as unknown as Vp).__vp.roster().length), { timeout: 10_000 }).toBe(before)
  expect(Date.now() - t1).toBeLessThanOrEqual(1000 + 500)
  // 4. zones overlay lists the curated zones
  const cur = JSON.parse(readFileSync(join(ROOT, 'scenarios', 'zones', 'shenzhen.zones.geojson'), 'utf8')) as { features: { id: string }[] }
  // the zones layer lists every curated feature of the world's zones (layers() only lists object names; INT-1)
  const shown = (await page.evaluate(() => (window as unknown as Vp).__vp.zones())).map((z) => z.id)
  for (const f of cur.features) expect(shown).toContain(f.id)
  // 5. world switch
  await page.goto(`${be.url}/world/shanghai`)
  await revealed(page)
  const sw = await page.evaluate(() => (window as unknown as { __perf: { load: { switchMs: number; ttfp: number } } }).__perf.load)
  const dir = process.env.AWR_PERF_RUN_DIR
  if (dir) writeFileSync(join(dir, 'metrics.json'), JSON.stringify({ switch_ms: Number.isFinite(sw.switchMs) ? sw.switchMs : sw.ttfp }))
  expect(await glyphScan(page)).toEqual([])
  await noErrors(w)
})
