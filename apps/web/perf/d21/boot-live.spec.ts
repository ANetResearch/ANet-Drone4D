// D2.1 boot screen on the real stack (WP-13b, M16; D2-AC-40 version line, D2-AC-41 five segments and the boot capacity line,
// D2-AC-42 page side; R-D2-29, R-D2-30, R-D2-32; AWR-04 §15.4, §15.5, §15.6.5). Backend: perf/d21/live21.ts (tests/e2e/sbstack.py,
// N_max = 1, RELEASE written from the build under test), the test build (D21_DIST) and the public demo build (D21_DEMO_DIST).
//   1. version line: with JavaScript disabled the static line equals build-info.json; the meta tag; five segments by weight;
//      the live capacity answer's `build` equals build-info.json field by field, so no "site updated" banner appears;
//   2. "you may have to queue" while loading: with the only sandbox slot held, the boot line shows the sandbox queue text and
//      the "开启沙盒需排队" badge before the reveal; requests while the mask is up are >= 5 s apart; after the reveal the start
//      Sheet shows "需排队 · 第 1 位"; the demo landing page shows the same and turns open once the slot is free;
//   3. an unreachable capacity endpoint shows "容量信息暂不可用" and never holds the reveal.
// Timing (round 2, d2-FX-R2-web-to-M16 item 1, d2-ACC-1-to-M16 item 2): the capacity request now leaves from the first inline
// classic script of <head> (app/boot/early.ts, mark `awr.boot.early`), not from the entry module; case 2 measures in the page
// (an init script's MutationObserver on the line, Resource Timing of the request and of the main chunks): the request starts
// before the main chunks have arrived (asserted always), <= 50 ms after the early mark and the line is written <= 100 ms
// after the answer (recorded always, asserted with M16_PERF=1, like the reveal degradation <= 5 %).
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import {
  baseUrl, buildInfo, createSession, display, endSession, ensureBuild, PERF, revealed, shot, skipOnboarding, startLive21,
  startLive21Preview, waitPoolEmpty, watch, type Held, type Live21,
} from './live21'

const PORT = 4194
const DEMO_PORT = 4195
let live: Live21 | null = null
let server: PreviewServer | null = null
let demo: PreviewServer | null = null
let TEST_DIST = ''
let DEMO_DIST = ''

test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 }, locale: 'zh-CN' })
test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => {
  test.setTimeout(600_000)
  TEST_DIST = ensureBuild('test')
  DEMO_DIST = ensureBuild('demo')
  live = await startLive21({ maxSessions: 1, releaseFrom: TEST_DIST })
  server = await startLive21Preview(PORT, live, TEST_DIST)
  demo = await startLive21Preview(DEMO_PORT, live, DEMO_DIST)
})
test.afterAll(async () => {
  await server?.close()
  await demo?.close()
  await live?.stop()
})

test('version line: static, equal to build-info.json, meta tag, five segments; the live capacity build matches', async ({ page, browser }) => {
  test.setTimeout(300_000)
  const b = buildInfo(TEST_DIST)
  const nojs = await browser.newContext({ javaScriptEnabled: false, baseURL: baseUrl(PORT), locale: 'zh-CN' })
  const p0 = await nojs.newPage()
  await p0.goto('/world/synthcity')
  await expect(p0.locator('#boot-mask .boot-version')).toHaveText(display(b))
  expect(await p0.locator('meta[name="awr-build"]').getAttribute('content')).toBe(`${b.version}-${b.channel}+${b.commit}${b.dirty ? '-dirty' : ''}`)
  const widths = await p0.locator('#boot-mask [data-seg]').evaluateAll((els) => els.map((e) => e.getBoundingClientRect().width))
  expect(widths).toHaveLength(5)
  const total = widths.reduce((a, x) => a + x, 0)
  ;[0.15, 0.1, 0.4, 0.3, 0.05].forEach((w, i) => expect(Math.abs(widths[i] - w * total)).toBeLessThanOrEqual(2))
  await nojs.close()

  const w = watch(page)
  const answers: Record<string, unknown>[] = []
  page.on('response', async (r) => {
    if (r.url().includes('/api/sandbox/v1/capacity') && r.status() === 200) answers.push(await r.json().catch(() => ({})))
  })
  await skipOnboarding(page)
  await page.goto('/world/synthcity')
  await expect(page.locator('#boot-mask .boot-version')).toHaveText(display(b))
  await expect.poll(() => answers.length, { timeout: 20_000 }).toBeGreaterThan(0)
  expect(answers[0].build).toEqual(b)                                           // D2-AC-40: field by field
  await revealed(page)
  await page.waitForTimeout(12_000)                                             // the banner appears <= 10 s after a mismatch
  await expect(page.locator('[data-update-banner]')).toHaveCount(0)
  expect(w.errors).toEqual([])
})

/** capacity requests while the boot mask is up, and the line text at the first hint */
async function bootWatch(page: Page): Promise<{ hits: number[]; maskUp: () => Promise<boolean> }> {
  const hits: number[] = []
  page.on('request', (r) => { if (r.url().includes('/api/sandbox/v1/capacity')) hits.push(Date.now()) })
  return { hits, maskUp: async () => (await page.locator('#boot-mask').count()) > 0 }
}

/** in-page record of the boot line's hint changes (performance.now()), installed before any page script */
async function lineClock(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __d21line: { t: number; v: string | null }[] }
    w.__d21line = []
    const look = (): void => {
      const v = document.querySelector('#boot-mask .boot-capacity')?.getAttribute('data-capacity-hint') ?? null
      const last = w.__d21line.at(-1)
      if (!last || last.v !== v) w.__d21line.push({ t: performance.now(), v })
    }
    new MutationObserver(look).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-capacity-hint'] })
  })
}

interface LineTiming { early: number | null; capStart: number | null; capEnd: number | null; mainEnd: number | null; shown: number | null }
/** D2-AC-41 in the page: the early mark, the first capacity request, the last main chunk (the modulepreload chunks and the
 *  entry of index.html) and the moment the line took `hint` */
async function lineTiming(page: Page, hint: string): Promise<LineTiming> {
  return page.evaluate((want) => {
    const res = performance.getEntriesByType('resource') as PerformanceResourceTiming[]
    const cap = res.filter((e) => e.name.includes('/api/sandbox/v1/capacity')).sort((a, b) => a.startTime - b.startTime)[0] ?? null
    const own = new Set<string>()
    for (const el of document.querySelectorAll<HTMLLinkElement | HTMLScriptElement>('link[rel="modulepreload"][href], script[type="module"][src]')) {
      own.add(new URL(el.getAttribute('href') ?? el.getAttribute('src') ?? '', location.href).href)
    }
    const main = res.filter((e) => own.has(e.name)).map((e) => e.responseEnd)
    const early = performance.getEntriesByName('awr.boot.early', 'mark')[0]?.startTime ?? null
    const shown = (window as unknown as { __d21line?: { t: number; v: string | null }[] }).__d21line?.find((x) => x.v === want)?.t ?? null
    return { early, capStart: cap?.startTime ?? null, capEnd: cap?.responseEnd ?? null, mainEnd: main.length ? Math.max(...main) : null, shown }
  }, hint)
}

test('the boot line shows the sandbox queue before the reveal; the start Sheet and the demo landing page agree', async ({ page, browser }) => {
  test.setTimeout(420_000)
  let held: Held | null = null
  try {
    held = await createSession(live!, 'MSIXBOOTFULLHINTAAAAAAAA', 't6_manual')            // the only slot
    const w = watch(page)
    await skipOnboarding(page)
    const bw = await bootWatch(page)
    await lineClock(page)
    await page.goto('/world/synthcity')
    const line = page.locator('#boot-mask .boot-capacity')
    await expect(line).toHaveAttribute('data-capacity-hint', 'queueSandbox', { timeout: 20_000 })
    expect(await bw.maskUp()).toBe(true)                                          // visible while loading (R-D2-32)
    await expect(line.locator('.boot-capacity-text')).toHaveText(/^沙盒 1 \/ 1 在用 · 排队 0 人 · (预计约 .+|预计等待暂无法估计)$/)
    await expect(line.locator('[data-capacity-queue]')).toHaveText('开启沙盒需排队')
    await shot(page, 'd21-boot-live-queue')
    await revealed(page)
    // D2-AC-41 timing in the page (round 2): request before the main chunks are in, shortly after the early script, line
    // written right after the answer
    const lt = await lineTiming(page, 'queueSandbox')
    test.info().annotations.push({ type: 'boot-capacity-timing', description: JSON.stringify(lt) })
    expect(lt.capStart, 'capacity request in Resource Timing').not.toBeNull()
    expect(lt.mainEnd, 'main chunks in Resource Timing').not.toBeNull()
    expect(lt.capStart!).toBeLessThan(lt.mainEnd!)
    expect(lt.shown, 'line switched to the queue hint').not.toBeNull()
    expect(lt.shown!).toBeGreaterThanOrEqual(lt.capEnd! - 1)
    if (PERF) {
      if (lt.early !== null) expect(lt.capStart! - lt.early).toBeLessThanOrEqual(50)
      expect(lt.shown! - lt.capEnd!).toBeLessThanOrEqual(100)
    }
    const during = bw.hits.slice()
    for (let i = 1; i < during.length; i++) expect(during[i] - during[i - 1]).toBeGreaterThanOrEqual(4900)
    test.info().annotations.push({ type: 'boot-capacity-requests', description: String(during.length) })
    // the start Sheet (a sandbox link the page does not hold)
    await page.goto('/sandbox/sb-aaaaaaaaaa?reveal=shell')
    await revealed(page)
    await expect(page.locator('[data-sandbox-unavailable]')).toBeVisible({ timeout: 30_000 })
    await page.locator('[data-sandbox-unavailable] button').last().click()
    await expect(page.locator('[data-start-sheet]')).toBeVisible()
    await expect(page.locator('[data-start-queue-hint]')).toHaveText(/^需排队 · 第 1 位 · /, { timeout: 15_000 })
    await shot(page, 'd21-boot-live-sheet-queue')
    await page.keyboard.press('Escape')
    expect(w.errors).toEqual([])

    // the public demo build's landing page: same line, then open once the slot is free (landing polls every 10 s)
    const dctx = await browser.newContext({ baseURL: baseUrl(DEMO_PORT), viewport: { width: 1600, height: 900 }, locale: 'zh-CN' })
    const dp = await dctx.newPage()
    const dw = watch(dp)
    await dp.goto('/')
    const dl = dp.locator('[data-landing-capacity]')
    await expect(dl).toHaveAttribute('data-landing-capacity', 'queueSandbox', { timeout: 20_000 })
    await expect(dl.locator('[data-capacity-queue]')).toHaveText('开启沙盒需排队')
    await expect(dp.locator('[data-landing-queue-hint]')).toHaveText(/^需排队 · 第 1 位 · /)
    await shot(dp, 'd21-boot-live-landing-queue')
    await endSession(live!, held)
    held = null
    await waitPoolEmpty(live!)
    await expect(dl).toHaveAttribute('data-landing-capacity', 'open', { timeout: 25_000 })
    await expect(dp.locator('[data-landing-queue-hint]')).toHaveCount(0)
    expect(dw.errors).toEqual([])
    await dctx.close()
  } finally {
    await endSession(live!, held)
    await waitPoolEmpty(live!).catch(() => null)
  }
})

test('an unreachable capacity endpoint shows "unavailable" and never holds the reveal', async ({ page, context }) => {
  test.setTimeout(300_000)
  const w = watch(page)
  await context.route('**/api/sandbox/v1/capacity', (r) => r.abort())
  await skipOnboarding(page)
  const t0 = Date.now()
  await page.goto('/world/synthcity')
  const line = page.locator('#boot-mask .boot-capacity')
  await expect(line).toHaveAttribute('data-capacity-hint', 'unavailable', { timeout: 15_000 }).catch(async () => {
    // the mask may already be gone on a warm cache: the reveal is what matters
    expect(await page.locator('#boot-mask').count()).toBe(0)
  })
  await revealed(page)
  test.info().annotations.push({ type: 'reveal_without_capacity_ms', description: String(Date.now() - t0) })
  expect(w.errors).toEqual([])
})
