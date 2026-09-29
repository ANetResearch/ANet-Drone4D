// Timeline and replay (D1-AC-18; M12-AC-004, 014, 016, 028, 047; content from M12-to-M16 item 3). Test build (window.__timeline,
// window.__perf.time), free-shenzhen on a live backend (harness AWR_PERF_BASE or a supervisor started here).
//   live: Space pauses -> after 500 ms __perf.time.tRenderS equals the TIME t_sim within 1 ms (frozen state, D decays);
//         ArrowRight / Shift+ArrowRight / Shift+Period step 100 ms / 1 s / 4 ms of simulation time while paused;
//   viewer: the clock controls are disabled for a viewer principal (seat not held).
// The replay part (deep link ?seg=0&t=420, 25 x Shift+Period, bookmarks, replay-worker kill) needs a recorded run and the
// replay-worker process; it runs under the harness with the ext process set and is marked fixme until M12's recording
// fixture for e2e (python -m awr.recorder.synth) is wired here.
import { expect, test } from '@playwright/test'
import { isTestBuild, liveBackend, revealed, watch, type Live } from './helpers'

let be: Live
test.beforeAll(async () => {
  be = await liveBackend('free-shenzhen')
})
test.afterAll(async () => {
  await be?.close()
})

type T = { __perf: { time?: { tRenderS: number; simNowS: number; state4: number; dGlobalMs: number } } }
const time = (page: import('@playwright/test').Page): Promise<{ tRenderS: number; simNowS: number; state4: number } | null> =>
  page.evaluate(() => (window as unknown as T).__perf.time ?? null)

test('live clock: pause freezes the render time, steps advance it', async ({ page }) => {
  test.setTimeout(180_000)
  await page.addInitScript(() => localStorage.setItem('awr.principal_hint', 'MSIXTEENTIMELINEGATEAAAA'))
  const w = watch(page)
  await page.goto(`${be.url}/world/shenzhen`)
  await revealed(page)
  test.skip(!(await isTestBuild(page)), 'needs a test build (window.__timeline); a skip is a failure in the harness')
  await expect.poll(async () => (await time(page))?.simNowS ?? 0, { timeout: 30_000 }).toBeGreaterThan(1)
  await page.mouse.move(640, 360) // locator.hover waits forever while a floating panel covers the canvas point (INT-1)
  await page.keyboard.press('Space')
  // the frozen state converges as D decays; at the 4 Hz frame rate of SwiftShader under load that takes longer than the
  // original fixed 500 ms wait (INT-1), so wait for convergence, then check that it stays frozen
  await expect.poll(async () => {
    const t = await time(page)
    return t ? Math.abs(t.tRenderS - t.simNowS) : 1
  }, { timeout: 10_000 }).toBeLessThanOrEqual(0.001)
  const a = await time(page)
  await page.waitForTimeout(500)
  const b = await time(page)
  expect(a && b).toBeTruthy()
  expect(Math.abs(b!.tRenderS - a!.tRenderS)).toBeLessThanOrEqual(0.001)
  expect(Math.abs(b!.tRenderS - b!.simNowS)).toBeLessThanOrEqual(0.001)
  for (const [key, dt] of [['ArrowRight', 0.1], ['Shift+ArrowRight', 1.0], ['Shift+Period', 0.004]] as const) {
    const t0 = (await time(page))!.simNowS
    await page.keyboard.press(key)
    await expect.poll(async () => (await time(page))!.simNowS - t0, { timeout: 10_000 }).toBeGreaterThanOrEqual(dt - 1e-6)
    expect((await time(page))!.simNowS - t0).toBeLessThanOrEqual(dt + 0.005)
  }
  await page.keyboard.press('Space')
  expect(w.errors, w.errors.join('\n')).toEqual([])
})

test('viewer cannot drive the clock', async ({ browser }) => {
  test.setTimeout(120_000)
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 } })
  const holder = await ctx.newPage()
  await holder.addInitScript(() => localStorage.setItem('awr.principal_hint', 'MSIXTEENTIMELINEGATEAAAA'))
  await holder.goto(`${be.url}/world/shenzhen`)
  await revealed(holder)
  const ctx2 = await browser.newContext({ viewport: { width: 1280, height: 720 } })
  const viewer = await ctx2.newPage()
  await viewer.addInitScript(() => localStorage.setItem('awr.principal_hint', 'MSIXTEENTIMELINEVIEWERAA'))
  await viewer.goto(`${be.url}/world/shenzhen`)
  await revealed(viewer)
  test.skip(!(await isTestBuild(viewer)), 'needs a test build')
  // state4 is 0 until the first TIME frame arrives; read the baseline once the clock is known (INT-1)
  await expect.poll(async () => (await time(viewer))?.state4 ?? 0, { timeout: 30_000 }).toBeGreaterThan(0)
  const t0 = (await time(viewer))?.state4
  await viewer.mouse.move(640, 360) // see above (INT-1)
  await viewer.keyboard.press('Space')
  await viewer.waitForTimeout(1000)
  expect((await time(viewer))?.state4).toBe(t0)
  await ctx.close()
  await ctx2.close()
})

test.fixme('replay: deep link, stepping, bookmarks and replay-worker loss', async () => {
  // M12-AC-028 / AC-047 / AC-026 / AC-051: needs a recorded run (python -m awr.recorder.synth) and --only sim-core,api,replay-worker
})
