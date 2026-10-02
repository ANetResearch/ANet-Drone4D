// Latency and interpolation (M16-FR-059; D1-AC-26; AWR-18 §7.3, §8.5): live S1 at x1, production build, UI only.
//   1. follow: select p600-01 (60 Hz channel), follow it (L), 30 s: t_sim -> pixel of the focus vehicle (__perf.latency),
//      selected-channel rate against the rAF rate and the connection's credit_skips per server tick (perf/server);
//   2. command to visible: hover (H) on p600-01 three times (the scenario takes the vehicle back after each one); each
//      command is marked cmd.sent by the M06 command path and closes when the control-owner switch reaches the rendered
//      time (M06 §6.16) -> snapshot.json;
//   3. focus-set switching (5 cycles): select the next vehicle, Third (close behind it), clear the selection (it joins
//      the focus set at close range), Orbit and home (it leaves);
//      latency.focusJumpM samples the position discontinuity at every entry and exit;
//   4. x10: three rate steps up (]), 20 s, HOLD frames / frames (the realtime x10 HOLD share), then back to x1.
// Phases 3 and 4 write metrics.json (focus_jump_max_m, hold_pct_x10); phase 1 also writes the selected-channel ratio and
// the credit-skip share. Thresholds are judged by the harness (perf/harness/cases/ui.mjs latency).
import { hotkey } from './fixtures/ui'
import { expect, test } from './fixtures/perf'

type Lat = { tSimToPixelMs: number[]; cmdToVisibleMs: number[]; focusJumpM: number[]; holdFrames: number; dGlobalMs: number }
type Net = { selectedHz: number; swarmHz: number; creditSkips: number }

test('latency: focus vehicle, command to visible, focus set, x10', async ({ perfPage }) => {
  test.setTimeout(240_000)
  const page = perfPage.page
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  const row = page.locator('[data-drone-id="p600-01"]').first()
  await row.waitFor({ timeout: 30_000 })
  await row.click()
  await hotkey(page, 'KeyL')
  await page.waitForTimeout(2000)
  // ---- 1. follow at x1
  const t0 = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { reset(s: string): void; frame: { count: number }; net: Net } }).__perf
    p.reset('latency')
    return { t: performance.now(), frames: p.frame.count, skips: p.net.creditSkips }
  })
  await page.waitForTimeout(30_000)
  const t1 = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { frame: { count: number }; net: Net } }).__perf
    return { t: performance.now(), frames: p.frame.count, skips: p.net.creditSkips, selHz: p.net.selectedHz, swarmHz: p.net.swarmHz }
  })
  const secs = (t1.t - t0.t) / 1000
  const rafHz = (t1.frames - t0.frames) / secs
  // perf/server credit_skips is per connection and counted per 60 Hz tick (AWR-17 §6.8, §6.9)
  const skipsPct = (100 * Math.max(0, t1.skips - t0.skips)) / (60 * secs)
  perfPage.writeMetrics({ selected_hz: t1.selHz, raf_hz: rafHz, selected_hz_over_raf: rafHz > 0 ? t1.selHz / rafHz : null, credit_skips_sel_pct: skipsPct })
  expect(t1.swarmHz).toBeGreaterThanOrEqual(9)
  // ---- 2. command to visible: hover on the followed vehicle three times through the product hotkey (single-vehicle path,
  // cmd.sent); the S1 scenario takes the vehicle back after each operator hover, so every press switches the control owner
  const cmdN = async (): Promise<number> => page.evaluate(() => (window as unknown as { __perf: { latency: { cmdToVisibleMs: { n: number } } } }).__perf.latency.cmdToVisibleMs.n)
  for (let k = 0; k < 3; k++) {
    const n0 = await cmdN()
    await hotkey(page, 'KeyH')
    await expect.poll(cmdN, { timeout: 6000, intervals: [500] }).toBeGreaterThan(n0).catch(() => undefined)
    await page.waitForTimeout(2500)
  }
  const snap = await perfPage.saveSnapshot()
  const lat = snap.latency as Lat
  expect(lat.tSimToPixelMs.length, 'focus vehicle latency samples').toBeGreaterThan(100)
  expect(lat.cmdToVisibleMs.length, 'command to visible samples').toBeGreaterThanOrEqual(2)
  // ---- 3. focus-set switching (home <-> focus on the selection): the camera distance moves the screen radii
  await page.evaluate(() => (window as unknown as { __perf: { reset(s: string): void } }).__perf.reset('latency'))
  // selected vehicles are not focus-set members (they run at 60 Hz) and the follow lock keeps the user's distance: put the
  // next vehicle in Third (close behind it), clear the selection (Third keeps its focus vehicle, which joins the set at
  // r_px >= 8 px), then orbit and home so it leaves (r_px < 6 px after the 1 s dwell)
  for (let k = 0; k < 5; k++) {
    await hotkey(page, 'Period') // select the next vehicle
    await page.waitForTimeout(300)
    await hotkey(page, 'Digit3') // Third
    await page.waitForTimeout(2500)
    await hotkey(page, 'Escape') // clear the selection
    await page.waitForTimeout(2500)
    await hotkey(page, 'Digit1') // Orbit
    await hotkey(page, 'Home')
    await page.waitForTimeout(3000)
  }
  const jumps = await page.evaluate(() => {
    const r = (window as unknown as { __perf: { latency: { focusJumpM: { buf: Float64Array; n: number } } } }).__perf.latency.focusJumpM
    return Array.from(r.buf.subarray(0, Math.min(r.n, r.buf.length)))
  })
  perfPage.writeMetrics({ focus_jump_max_m: jumps.length ? Math.max(...jumps) : null, focus_jump_n: jumps.length })
  // ---- 4. x10 realtime: HOLD share of the frames
  for (let k = 0; k < 3; k++) {
    await hotkey(page, 'BracketRight')
    await page.waitForTimeout(300)
  }
  await expect.poll(() => page.evaluate(() => (window as unknown as { __perf: { time?: { rate: number } } }).__perf.time?.rate ?? 0), { timeout: 10_000 }).toBeGreaterThanOrEqual(9.99)
  await page.waitForTimeout(3000)
  const h0 = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { frame: { count: number }; latency: { holdFrames: number } } }).__perf
    return { frames: p.frame.count, hold: p.latency.holdFrames }
  })
  await page.waitForTimeout(20_000)
  const h1 = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { frame: { count: number }; latency: { holdFrames: number } } }).__perf
    return { frames: p.frame.count, hold: p.latency.holdFrames }
  })
  const fr = h1.frames - h0.frames
  perfPage.writeMetrics({ hold_pct_x10: fr > 0 ? (100 * (h1.hold - h0.hold)) / fr : null, x10_frames: fr })
  for (let k = 0; k < 3; k++) await hotkey(page, 'BracketLeft')
  expect(fr).toBeGreaterThan(50)
  perfPage.assertNoPageErrors()
})
