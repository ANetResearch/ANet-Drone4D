// Power save on the real stack (WP-13b, M16; D2-AC-44 and D2-AC-45 page side; R-D2-33, R-D2-34; AWR-04 §15.7). Backend:
// perf/d21/live21.ts (tests/e2e/sbstack.py, N_max = 1, server power thresholds viewer 20 s, sandbox 20 s, warning 5 s, hidden
// 4 s, published through the public capacity endpoint).
//   1. shared show viewer (test build, `?power=viewer` with the same thresholds): countdown at idle - warn, saving at idle: the
//      show-api connection is gone within 2 s (R58 /api/rt/inspect; the page's realtime socket lives in a Worker), no frame and
//      no request at all for 10 s (round 2: the plain overlay no longer polls the capacity endpoint, WP-12c round 2; only the
//      queue cards and the start Sheet hold a polling lease), the overlay is up; the first drag
//      resumes: the link is LIVE again, show-api counts the connection again, frames flow and the state is ACTIVE;
//   2. sandbox operator (test build, a real session held by the page): saving parks the session through SB-69 (the pool
//      reports PARKED, reason idle, slot released); another visitor takes the only slot; any key resumes through SB-70: 202 and
//      the resume queue card ("恢复沙盒需排队 · 第 1 位") inside the power overlay; when the other session ends the page claims
//      the slot by itself (page visible) and is ACTIVE again on the same sid, epoch + 1;
//   3. public demo build: the shared show follows the server's thresholds from the capacity endpoint (no test switches):
//      countdown banner and saving overlay, resumed by a drag;
//   4. sandbox with an external API client (issue #59): session REST calls without X-AWR-Client every 4 s for twice the idle
//      threshold keep the page out of the countdown and of saving; once they stop the page saves and the session parks (idle).
// The frame and request bounds of D2-AC-44 are checked in every run; the main-thread share while saving (Chromium TaskDuration)
// is recorded always and asserted with M16_PERF=1 over 60 s (<= 1 %, D2-AC-46 P1, which implies the 10 s <= 2 % of D2-AC-44);
// the server-side savings are measured by tools/bench/sandbox/power.py.
// Round 2: the V8 garbage collection inside the window (a Major GC of the memory reducer usually follows the drop of the
// allocation rate, 65-140 ms, d2-FX-R2-web-to-M16 item 7) is traced and reported apart (MajorGC, MinorGC and V8.GC_* on the
// renderer main thread, union of the intervals); the M16_PERF bound applies to the share without it and the raw share is
// recorded next to it. The cases are independent (each brings its own state back to an empty pool): no serial mode, so a
// failed bound in one case no longer skips the others (d2-ACC-1-to-M16 item 3; a failure restarts the worker and the stack).
import { expect, test, type CDPSession, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import {
  baseUrl, capacity, createSession, PERF, showClients, endSession, ensureBuild, holdSession, isTestBuild, pageHeld, revealed, session, shot,
  skipOnboarding, startLive21, startLive21Preview, waitPoolEmpty, waitState, watch, type Held, type Live21,
} from './live21'

const PORT = 4196
const DEMO_PORT = 4197
const POWER = { viewer_idle_s: 20, viewer_idle_busy_s: 20, sandbox_idle_s: 20, sandbox_idle_busy_s: 20, warn_s: 5, hidden_s: 4, quiet_s: 8, grace_s: 10 }
const Q = 'powerIdle=20&powerWarn=5&powerQuiet=8&powerHidden=4'
let live: Live21 | null = null
let server: PreviewServer | null = null
let demo: PreviewServer | null = null

test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 }, locale: 'zh-CN' })
test.beforeAll(async () => {
  test.setTimeout(600_000)
  const dist = ensureBuild('test')
  const demoDist = ensureBuild('demo')
  live = await startLive21({ maxSessions: 1, power: POWER, releaseFrom: dist })
  server = await startLive21Preview(PORT, live, dist)
  demo = await startLive21Preview(DEMO_PORT, live, demoDist)
})
test.afterAll(async () => {
  await server?.close()
  await demo?.close()
  await live?.stop()
})

type Ux = { __ux: { power: { state: string; transitions: string[] } }; __vp?: { loop: { stats(): { frameNo: number; suspended: boolean } } } }
const powerState = (page: Page): Promise<string> => page.evaluate(() => (window as unknown as Ux).__ux.power.state)
const frameNo = (page: Page): Promise<number> => page.evaluate(() => (window as unknown as Ux).__vp?.loop.stats().frameNo ?? -1)
const conn = (page: Page): Promise<string> => page.evaluate(() => String((window as unknown as { __ux: { power: { conn?: string } } }).__ux.power.conn ?? ''))

/** the renderer main thread's garbage collection while `run` lasts (ms; union of MajorGC, MinorGC and V8.GC_* intervals) */
async function tracedGc(cdp: CDPSession, run: () => Promise<void>): Promise<number> {
  type Ev = { name: string; ph: string; ts: number; dur?: number; pid: number; tid: number; args?: { name?: string } }
  const events: Ev[] = []
  const onData = (e: { value: Record<string, unknown>[] }): void => { events.push(...(e.value as unknown as Ev[])) }
  cdp.on('Tracing.dataCollected', onData)
  await cdp.send('Tracing.start', { categories: 'devtools.timeline,v8,disabled-by-default-v8.gc,__metadata', transferMode: 'ReportEvents' } as never)
  try {
    await run()
  } finally {
    const done = new Promise<void>((ok) => cdp.once('Tracing.tracingComplete', () => ok()))
    await cdp.send('Tracing.end')
    await done
    cdp.off('Tracing.dataCollected', onData)
  }
  const main = new Set(events.filter((e) => e.ph === 'M' && e.name === 'thread_name' && e.args?.name === 'CrRendererMain').map((e) => `${e.pid}:${e.tid}`))
  const iv: [number, number][] = []
  const open = new Map<string, number[]>()
  for (const e of events) {
    if (!main.has(`${e.pid}:${e.tid}`) || !/^(MajorGC|MinorGC|V8\.GC_)/.test(e.name)) continue
    const k = `${e.pid}:${e.tid}:${e.name}`
    if (e.ph === 'X' && e.dur) iv.push([e.ts, e.ts + e.dur])
    else if (e.ph === 'B') open.set(k, [...(open.get(k) ?? []), e.ts])
    else if (e.ph === 'E') { const b = open.get(k)?.pop(); if (b !== undefined) iv.push([b, e.ts]) }
  }
  iv.sort((a, b) => a[0] - b[0])
  let total = 0
  let cur: [number, number] | null = null
  for (const x of iv) {
    if (cur && x[0] <= cur[1]) cur[1] = Math.max(cur[1], x[1])
    else { if (cur) total += cur[1] - cur[0]; cur = [x[0], x[1]] }
  }
  if (cur) total += cur[1] - cur[0]
  return total / 1000
}

async function drag(page: Page): Promise<void> {
  await page.mouse.move(700, 420)
  await page.mouse.down()
  await page.mouse.move(780, 450, { steps: 6 })
  await page.mouse.up()
}

test('shared show viewer: countdown, saving closes the link without frames or requests, the first drag reconnects', async ({ page }) => {
  test.setTimeout(300_000)
  const w = watch(page)
  await skipOnboarding(page)
  const n0 = (await showClients(live!)).n
  await page.goto(`/world/synthcity?power=viewer&${Q}`)
  await revealed(page)
  test.skip(!(await isTestBuild(page)), 'needs a test build')
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  await expect.poll(() => conn(page), { timeout: 60_000 }).toMatch(/^LIVE/)
  await expect.poll(async () => (await showClients(live!)).n, { timeout: 10_000 }).toBe(n0 + 1)
  await page.mouse.move(600, 400)
  await page.mouse.move(820, 460, { steps: 5 })
  const t0 = Date.now()
  await expect(page.locator('[data-power-banner]')).toBeVisible({ timeout: 25_000 })
  const tWarn = (Date.now() - t0) / 1000
  await expect(page.locator('[data-power-overlay="SAVING"]')).toBeVisible({ timeout: 15_000 })
  const tSave = (Date.now() - t0) / 1000
  test.info().annotations.push({ type: 'viewer_warn_save_s', description: `${tWarn.toFixed(1)} / ${tSave.toFixed(1)}` })
  expect(tWarn).toBeGreaterThanOrEqual(13)
  expect(tSave).toBeGreaterThanOrEqual(18)
  // the server side: the connection is gone within 2 s (D2-AC-44)
  const tGone = Date.now()
  await expect.poll(async () => (await showClients(live!)).n, { timeout: 5_000, intervals: [250] }).toBe(n0)
  test.info().annotations.push({ type: 'viewer_conn_gone_ms', description: String(Date.now() - tGone) })
  await page.waitForTimeout(600)
  const f0 = await frameNo(page)
  const reqs: string[] = []
  const onReq = (r: { url(): string }) => { reqs.push(r.url()) }
  page.on('request', onReq)
  // main-thread busy share from the Chromium metrics (TaskDuration): <= 2 % over 10 s (D2-AC-44), <= 1 % over 60 s (D2-AC-46,
  // P1); timing bounds only with M16_PERF=1 (the share is recorded either way)
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Performance.enable')
  const task = async (): Promise<number> => ((await cdp.send('Performance.getMetrics')).metrics.find((m) => m.name === 'TaskDuration')?.value ?? 0)
  const b0 = await task()
  const tw = Date.now()
  let span = 0
  const gcMs = await tracedGc(cdp, async () => {
    await page.waitForTimeout(PERF ? 60_000 : 10_000)
    span = (Date.now() - tw) / 1000
  })
  const busy = (await task() - b0) / span
  const gcShare = gcMs / 1000 / span
  const net = Math.max(0, busy - gcShare)
  test.info().annotations.push({ type: 'saving_main_thread_busy',
    description: `${(busy * 100).toFixed(2)} % over ${PERF ? 60 : 10} s, of which GC ${gcMs.toFixed(0)} ms (${(gcShare * 100).toFixed(2)} %), without GC ${(net * 100).toFixed(2)} %` })
  if (PERF) expect(net).toBeLessThanOrEqual(0.01)
  page.off('request', onReq)
  expect((await frameNo(page)) - f0).toBeLessThanOrEqual(1)
  expect(reqs.filter((u) => !u.startsWith('data:'))).toEqual([])
  await shot(page, 'd21-power-live-viewer-saving')
  await drag(page)
  await expect.poll(() => powerState(page), { timeout: 30_000 }).toBe('ACTIVE')
  await expect.poll(() => conn(page), { timeout: 15_000 }).toMatch(/^LIVE/)
  await expect.poll(async () => (await showClients(live!)).n, { timeout: 10_000 }).toBe(n0 + 1)
  await expect(page.locator('[data-power-overlay]')).toHaveCount(0)
  // frames flow again (the loop is no longer suspended)
  const f1 = await frameNo(page)
  await page.waitForTimeout(1500)
  expect((await frameNo(page)) - f1).toBeGreaterThan(5)
  const tr = await page.evaluate(() => (window as unknown as Ux).__ux.power.transitions)
  expect(tr).toContain('WARNING>SAVING:idle')
  expect(tr.some((x) => x.startsWith('RESUMING>ACTIVE'))).toBe(true)
  expect(w.errors).toEqual([])
})

test('sandbox operator: parked through SB-69, resume queues behind a full pool, claims the slot and comes back', async ({ page }) => {
  test.setTimeout(420_000)
  const w = watch(page)
  let a: Held | null = null
  let b: Held | null = null
  try {
    a = await createSession(live!, 'MSIXPOWERAHINTAAAAAAAAAA', 't1_point_watch')
    await waitState(live!, a, ['READY', 'ACTIVE'])
    await holdSession(page, a)
    await page.goto(`/sandbox/${a.sid}?power=sandbox&${Q}`)
    await revealed(page)
    test.skip(!(await isTestBuild(page)), 'needs a test build')
    await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
    await waitState(live!, a, ['ACTIVE'])
    const epoch0 = Number((await session(live!, a)).epoch)
    await page.mouse.move(600, 400)
    await page.mouse.move(820, 460, { steps: 5 })
    await expect(page.locator('[data-power-overlay="SAVING"]')).toBeVisible({ timeout: 40_000 })
    await expect(page.locator('[data-power-role="sandbox"]')).toContainText('沙盒已保存并暂停')
    const parked = await waitState(live!, a, ['PARKED'], 30_000)
    expect(parked.park_reason).toBe('idle')
    await expect.poll(async () => Number((await capacity(live!)).sandbox.active), { timeout: 10_000 }).toBe(0)
    await shot(page, 'd21-power-live-sandbox-parked')
    // another visitor takes the only slot
    b = await createSession(live!, 'MSIXPOWERBHINTAAAAAAAAAA', 't6_manual')
    await waitState(live!, b, ['READY', 'ACTIVE'])
    // any key: SB-70 -> 202 -> the resume queue card inside the overlay
    await page.keyboard.press('KeyX')
    const card = page.locator('[data-queue-kind="resume"]')
    await expect(card).toBeVisible({ timeout: 30_000 })
    await expect(card).toContainText('恢复沙盒需排队 · 第 1 位')
    expect(await powerState(page)).toBe('QUEUED')
    await shot(page, 'd21-power-live-sandbox-resume-queue')
    await endSession(live!, b)
    b = null
    // the page claims the slot by itself (visible) and is back on the same sid
    await expect.poll(() => powerState(page), { timeout: 120_000 }).toBe('ACTIVE')
    await expect(card).toHaveCount(0)
    const h = await pageHeld(page)
    expect(h?.sid).toBe(a.sid)
    a = { sid: a.sid, token: h!.token }
    const s = await waitState(live!, a, ['READY', 'ACTIVE'])
    expect(Number(s.epoch)).toBe(epoch0 + 1)
    await expect(page.locator('[data-session-bar]')).toBeVisible()
    await expect(page.locator('[data-power-overlay]')).toHaveCount(0)
    expect(w.errors).toEqual([])
  } finally {
    const h = await pageHeld(page).catch(() => null)
    await endSession(live!, h ?? a)
    await endSession(live!, b)
    await waitPoolEmpty(live!).catch(() => null)
  }
})

test('sandbox: external API calls hold off power saving; the page saves once they stop (issue #59)', async ({ page }) => {
  test.setTimeout(300_000)
  const w = watch(page)
  let a: Held | null = null
  try {
    a = await createSession(live!, 'MSIXPOWERCHINTAAAAAAAAAA', 't1_point_watch')
    await waitState(live!, a, ['READY', 'ACTIVE'])
    await holdSession(page, a)
    await page.goto(`/sandbox/${a.sid}?power=sandbox&${Q}`)
    await revealed(page)
    test.skip(!(await isTestBuild(page)), 'needs a test build')
    await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
    await waitState(live!, a, ['ACTIVE'])
    // an SDK-like client (no X-AWR-Client header) calls the session API every 4 s for twice the idle threshold
    const end = Date.now() + 2 * POWER.sandbox_idle_s * 1000
    while (Date.now() < end) {
      const r = await fetch(`${live!.base}/api/sandbox/v1/sessions/${a.sid}/vehicles`, { headers: { authorization: `Bearer ${a.token}` } })
      expect(r.status).toBe(200)
      expect(['ACTIVE', 'QUIET']).toContain(await powerState(page))
      await page.waitForTimeout(4_000)
    }
    await expect(page.locator('[data-power-overlay]')).toHaveCount(0)
    // the calls stop: the countdown, then saving after the idle threshold, and the session parks with reason idle
    await expect(page.locator('[data-power-overlay="SAVING"]')).toBeVisible({ timeout: 40_000 })
    const parked = await waitState(live!, a, ['PARKED'], 30_000)
    expect(parked.park_reason).toBe('idle')
    expect(w.errors).toEqual([])
  } finally {
    const h = await pageHeld(page).catch(() => null)
    await endSession(live!, h ?? a)
    await waitPoolEmpty(live!).catch(() => null)
  }
})

test('public demo build: the shared show follows the server thresholds, saves and resumes', async ({ browser }) => {
  test.setTimeout(300_000)
  const ctx = await browser.newContext({ baseURL: baseUrl(DEMO_PORT), viewport: { width: 1600, height: 900 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  const w = watch(page)
  await skipOnboarding(page)
  const n0 = (await showClients(live!)).n
  await page.goto('/world/synthcity')
  await revealed(page)
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
  await expect.poll(async () => (await showClients(live!)).n, { timeout: 60_000 }).toBe(n0 + 1)
  await page.mouse.move(600, 400)
  await page.mouse.move(820, 460, { steps: 5 })
  await expect(page.locator('[data-power-banner]')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-power-overlay="SAVING"]')).toBeVisible({ timeout: 15_000 })
  await expect(page.locator('[data-power-build]')).toContainText('V0.2.0-demo')
  await expect.poll(async () => (await showClients(live!)).n, { timeout: 5_000, intervals: [250] }).toBe(n0)
  await shot(page, 'd21-power-live-demo-saving')
  await drag(page)
  await expect(page.locator('[data-power-overlay]')).toHaveCount(0, { timeout: 30_000 })
  await expect.poll(async () => (await showClients(live!)).n, { timeout: 15_000 }).toBe(n0 + 1)
  expect(w.errors).toEqual([])
  await ctx.close()
})
