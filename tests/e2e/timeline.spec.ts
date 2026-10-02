// Timeline and replay (D1-AC-18; M12-AC-004, 014, 016, 028, 047; content from M12-to-M16 item 3). Test build (window.__timeline,
// window.__perf.time), free-shenzhen on a live backend (harness AWR_PERF_BASE or a supervisor started here).
//   live: Space pauses -> after 500 ms __perf.time.tRenderS equals the TIME t_sim within 1 ms (frozen state, D decays);
//         ArrowRight / Shift+ArrowRight / Shift+Period step 100 ms / 1 s / 4 ms of simulation time while paused;
//   viewer: the clock controls are disabled for a viewer principal (seat not held).
// The replay part (M12-AC-028, AC-047, AC-026, AC-051; M11-AC-042) always starts its own backend: a supervisor
// (ci profile, --only sim-core,api,replay-worker) whose runs directory holds one synthetic recording written by M12's
// fixture `python -m awr.recorder.synth` (apps/web/perf/m12/common.ts startReplayBackend; 8 vehicles, 480 s, run
// r20260929-010203-abcd): the deep link /world/shenzhen/replay/<run>?seg=0&t=420 opens the replay at 420 s; 25 x
// Shift+Period move 1.00 s (25 blocks of 40 ms), -> and <- move +-1 s; a bookmark written to the run before the replay
// (as during the live recording) is listed and reached by PageDown, M adds one at the seen time; kill -9 of the
// replay-worker shows "回放已停止" within 1 s (+0.5 s polling granularity) and the session returns to live. The api
// serves the test build of AWR_WEB_DIST (or apps/web/dist).
import { expect, test } from '@playwright/test'
import { RUN, startReplayBackend, type ReplayBackend } from '../../apps/web/perf/m12/common'
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
    // the step guard reads the store clock state (<= 4 Hz): a key pressed while it still says STEPPING is refused
    // (hint.pauseFirst), so wait until the store shows PAUSED with no pending control, as the readout does (FX-GW)
    await expect.poll(() => page.evaluate(() => {
      const st = (window as unknown as { __timeline: { store: { getState(): { state4: number; pending: unknown } } } }).__timeline.store.getState()
      return st.state4 === 2 && st.pending == null
    }), { timeout: 15_000 }).toBe(true)
    const t0 = (await time(page))!.simNowS
    await page.keyboard.press(key)
    await expect.poll(async () => (await time(page))!.simNowS - t0, { timeout: 10_000 }).toBeGreaterThanOrEqual(dt - 1e-6)
    expect((await time(page))!.simNowS - t0).toBeLessThanOrEqual(dt + 0.005)
  }
  await page.keyboard.press('Space')
  expect(w.errors, w.errors.join('\n')).toEqual([])
})

test('viewer cannot drive the clock', async ({ browser }) => {
  // two SwiftShader pages render at once: under the full parallel load of the acceptance phase (load 20+, 5 s frame
  // intervals) the second page needs more than the default 60 s to reveal; the limits only guard against hangs (FX-GW)
  test.setTimeout(300_000)
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 } })
  const holder = await ctx.newPage()
  await holder.addInitScript(() => localStorage.setItem('awr.principal_hint', 'MSIXTEENTIMELINEGATEAAAA'))
  await holder.goto(`${be.url}/world/shenzhen`)
  await revealed(holder, 120_000)
  const ctx2 = await browser.newContext({ viewport: { width: 1280, height: 720 } })
  const viewer = await ctx2.newPage()
  await viewer.addInitScript(() => localStorage.setItem('awr.principal_hint', 'MSIXTEENTIMELINEVIEWERAA'))
  await viewer.goto(`${be.url}/world/shenzhen`)
  await revealed(viewer, 150_000)
  test.skip(!(await isTestBuild(viewer)), 'needs a test build')
  // state4 is 0 until the first TIME frame arrives; read the baseline once the clock is known (INT-1)
  await expect.poll(async () => (await time(viewer))?.state4 ?? 0, { timeout: 60_000 }).toBeGreaterThan(0)
  const t0 = (await time(viewer))?.state4
  await viewer.mouse.move(640, 360) // see above (INT-1)
  await viewer.keyboard.press('Space')
  await viewer.waitForTimeout(1000)
  expect((await time(viewer))?.state4).toBe(t0)
  await ctx.close()
  await ctx2.close()
})

type TL = { __timeline: { store: { getState(): Record<string, unknown> }; actions: Record<string, (...a: unknown[]) => unknown> } }
interface Replay { mode: string; status: string | null; code: number | null; bookmarks: { tS: number; label: string }[] }
const replayState = (page: import('@playwright/test').Page): Promise<Replay> => page.evaluate(() => {
  const s = (window as unknown as TL).__timeline.store.getState() as { mode: string; playback: { status: string; code: number | null } | null; bookmarks: { tS: number; label: string }[] }
  return { mode: s.mode, status: s.playback?.status ?? null, code: s.playback?.code ?? null, bookmarks: s.bookmarks.map((b) => ({ tS: b.tS, label: b.label })) }
})
const REPLAY_HINT = 'MSIXTEENREPLAYGATEAAAAAA'
// The replay step keys seek relative to the shown time (store tDisplayS, written at <= 4 Hz, M12-AC-020), which follows
// the clock up to 250 ms after a seek lands; a key pressed in that window steps from the previous position (421.04 instead
// of 422 after 25 x Shift+. then ->, seen under load). Like an operator who reads the readout first, wait until the shown
// time equals the clock and no seek is pending before the next key (FX-GW; frontend request FX-GW-to-M12-M15).
const settled = (page: import('@playwright/test').Page): Promise<boolean> => page.evaluate(() => {
  const s = (window as unknown as TL).__timeline.store.getState() as { tDisplayS: number; pending: unknown }
  const t = (window as unknown as T).__perf.time
  return !!t && s.pending == null && Math.abs(s.tDisplayS - t.simNowS) <= 1e-3
})
const settle = (page: import('@playwright/test').Page): Promise<void> =>
  expect.poll(() => settled(page), { timeout: 15_000 }).toBe(true)
const BLOCK_S = 0.04 // x1 recording block interval (M12-FR-032): one Shift+Period step

async function token(base: string, role: string, hint?: string): Promise<string> {
  const r = await fetch(`${base}/api/auth/token`, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ role, ...(hint ? { principal_hint: hint } : {}) }) })
  expect(r.status, await r.clone().text()).toBe(200)
  return ((await r.json()) as { token: string }).token
}

test.describe('replay', () => {
  let rb: ReplayBackend | null = null
  test.beforeAll(async () => {
    rb = await startReplayBackend({ n: 8, simS: 480 })
  })
  test.afterAll(async () => {
    await rb?.close()
  })

  test('replay: deep link, stepping, bookmarks and replay-worker loss', async ({ page }) => {
    test.setTimeout(300_000)
    const base = rb!.url
    // a shared bookmark written to the recorded run before the replay opens (as an operator does during the live run)
    const op = await token(base, 'operator', REPLAY_HINT)
    const auth = { authorization: `Bearer ${op}`, 'content-type': 'application/json' }
    const bm = await fetch(`${base}/api/runs/${RUN}/bookmarks`, { method: 'POST', headers: auth,
      body: JSON.stringify({ segment: 0, t_sim_ns: 433_500_000_000, label: 'live-mark' }) })
    expect(bm.status, await bm.clone().text()).toBe(201)

    await page.addInitScript((h) => localStorage.setItem('awr.principal_hint', h), REPLAY_HINT)
    const w = watch(page)
    await page.goto(`${base}/world/shenzhen/replay/${RUN}?seg=0&t=420`)
    await revealed(page)
    test.skip(!(await isTestBuild(page)), 'needs a test build (window.__timeline); a skip is a failure in the harness')
    // M12-AC-028: the deep link opens the replay paused at 420 s
    await expect.poll(async () => (await replayState(page)).status, { timeout: 60_000 }).toBe('paused')
    expect((await replayState(page)).mode).toBe('replay')
    await expect.poll(async () => {
      const t = await time(page)
      return t ? Math.abs(t.tRenderS - 420) : 1e9
    }, { timeout: 30_000 }).toBeLessThanOrEqual(BLOCK_S + 1e-3)
    // M12-AC-047: 25 x Shift+. = 1.00 s (one block each, on the block grid); -> +1 s, <- -1 s
    await page.mouse.move(640, 360) // keys go to the page (see the live test)
    await settle(page)
    const t0 = (await time(page))!.simNowS
    for (let i = 0; i < 25; i++) await page.keyboard.press('Shift+Period')
    await expect.poll(async () => Math.abs((await time(page))!.simNowS - (t0 + 1.0)), { timeout: 30_000 }).toBeLessThanOrEqual(BLOCK_S / 2)
    for (const [key, dt] of [['ArrowRight', 1], ['ArrowLeft', -1]] as const) {
      await settle(page)
      const a = (await time(page))!.simNowS
      await page.keyboard.press(key)
      await expect.poll(async () => Math.abs((await time(page))!.simNowS - (a + dt)), { timeout: 30_000 }).toBeLessThanOrEqual(BLOCK_S / 2)
    }
    // M12-AC-026: the bookmark written before the replay is listed and PageDown reaches it
    await expect.poll(async () => (await replayState(page)).bookmarks.some((b) => b.label === 'live-mark' && Math.abs(b.tS - 433.5) < 1e-6),
      { timeout: 30_000 }).toBe(true)
    await settle(page)
    await page.keyboard.press('Shift+ArrowRight') // +10 s: close to the mark, fewer event markers on the way
    await expect.poll(async () => (await time(page))!.simNowS, { timeout: 30_000 }).toBeGreaterThan(430)
    const stops: number[] = []
    for (let i = 0; i < 80; i++) {
      await settle(page)
      const before = (await time(page))!.simNowS
      if (Math.abs(before - 433.5) <= BLOCK_S) break
      await page.keyboard.press('PageDown')
      await expect.poll(async () => (await time(page))!.simNowS, { timeout: 30_000 }).not.toBe(before)
      stops.push((await time(page))!.simNowS)
    }
    expect(Math.abs((await time(page))!.simNowS - 433.5), stops.join(', ')).toBeLessThanOrEqual(BLOCK_S)
    // M adds a shared bookmark at the seen time (runs REST); Esc closes its label editor
    await settle(page)
    await page.keyboard.press('KeyM')
    await expect.poll(async () => {
      const r = await fetch(`${base}/api/runs/${RUN}/bookmarks`, { headers: auth })
      return ((await r.json()) as { items: unknown[] }).items.length
    }, { timeout: 15_000 }).toBe(2)
    await page.keyboard.press('Escape')
    // M12-AC-051: kill -9 of the replay-worker -> playbackState{error, 213} and the "replay stopped" toast within 1 s
    const procs = await fetch(`${base}/api/sys/procs`, { headers: { authorization: `Bearer ${await token(base, 'viewer')}` } })
    const rw = ((await procs.json()) as { items: { name: string; pid: number | null }[] }).items.find((x) => x.name === 'replay-worker')
    expect(rw?.pid, 'replay-worker pid').toBeGreaterThan(0)
    const k0 = Date.now()
    process.kill(rw!.pid!, 'SIGKILL')
    await expect.poll(async () => {
      const r = await replayState(page)
      return `${r.status}:${r.code}`
    }, { timeout: 5_000, intervals: [50] }).toBe('error:213')
    const lostMs = Date.now() - k0
    await expect(page.getByText('回放已停止').first()).toBeVisible({ timeout: 1_000 })
    expect(lostMs).toBeLessThanOrEqual(1_500) // 1 s + polling granularity (INT-1 convention)
    // back to live: the gateway does not wait for the lost worker
    await page.evaluate(() => (window as unknown as TL).__timeline.actions.closeReplay())
    await expect.poll(async () => (await replayState(page)).mode, { timeout: 15_000 }).toBe('live')
    expect(w.errors, w.errors.join('\n')).toEqual([])
  })
})
