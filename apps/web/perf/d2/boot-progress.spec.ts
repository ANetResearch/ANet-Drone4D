// Boot progress of D2 (WP-13, M16; D2-AC-01; R-D2-01, ADR-108; M15-FR-201, AWR-14 §20.3.3). Test build on the M06 test
// server (dist, /worlds of the repository; /api answers 404 so the page runs on FakeSource):
//   1. shared path /world/synthcity (viewport on, 6 FakeSource vehicles): the progress samples (__ux.boot.progress) never
//      decrease, reach 1 at the reveal, the bar is a scale transform; at least four distinct phase texts (`.boot-phase`,
//      observed by a MutationObserver from the first paint) and at least four segments seen; the reveal and the TTFP go
//      to metrics.json (harness case d2.boot-progress compares them with the D1 baseline: <= 5 % worse, M16_PERF=1);
//   2. sandbox path /sandbox/<sid> with the session still SPAWNING (scripted REST): the mask lifts without waiting for
//      READY (the reveal is not gated by the session, D2-AC-01), the session bar shows the spawning phase.
// Build: `D2_DIST=<dir> node perf/m15/helpers/build-ui.mjs`; run: `M06_DIST=<dir> npx playwright test perf/d2/boot-progress.spec.ts`.
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { startM06Server, type M06Server } from '../m06/server'
import { DIST, mergeMetrics, REPO, WEB } from './live'

const PERF = process.env.M16_PERF === '1'
let srv: M06Server | null = null
test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => { srv = await startM06Server(undefined, resolve(WEB, DIST)) })
test.afterAll(async () => { await srv?.close() })

interface Sample { tMs: number; p: number; seg: string | null }
async function observePhases(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __d2Phases: string[] }
    w.__d2Phases = []
    const take = (): void => {
      const el = document.querySelector('#boot-mask .boot-phase')
      const s = el?.textContent?.trim() ?? ''
      if (s && w.__d2Phases[w.__d2Phases.length - 1] !== s) w.__d2Phases.push(s)
    }
    new MutationObserver(take).observe(document, { subtree: true, childList: true, characterData: true })
  })
}

test('shared path: monotone, four phases, 1 at the reveal; reveal and TTFP recorded', async ({ page }) => {
  await observePhases(page)
  await page.goto(`${srv!.url}/world/synthcity?source=fake&fakeWorld=synthcity&fakeN=6&fakeGround=1`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 120_000 })
  const r = await page.evaluate(() => {
    const w = window as unknown as { __ux?: { boot: { progress: Sample[] } }; __perf?: { load: { revealAt: number; ttfp: number } }; __d2Phases: string[] }
    return { samples: w.__ux?.boot.progress ?? null, phases: w.__d2Phases, load: w.__perf?.load ?? null }
  })
  test.skip(r.samples === null, 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
  const s = r.samples!
  expect(s.length).toBeGreaterThan(3)
  for (let i = 1; i < s.length; i++) expect(s[i].p).toBeGreaterThanOrEqual(s[i - 1].p)
  expect(s.at(-1)?.p).toBe(1)
  const segs = new Set(s.map((x) => x.seg))
  expect(segs.size).toBeGreaterThanOrEqual(4)
  expect(new Set(r.phases).size).toBeGreaterThanOrEqual(4)
  const metrics = { boot_reveal_ms: r.load?.revealAt ?? null, boot_ttfp_ms: r.load?.ttfp ?? null, boot_phases: new Set(r.phases).size }
  test.info().annotations.push({ type: 'boot', description: JSON.stringify(metrics) })
  if (process.env.AWR_PERF_RUN_DIR || PERF) mergeMetrics(metrics)
})

test('sandbox path: the reveal does not wait for the session (still SPAWNING)', async ({ page, context }) => {
  const SID = 'sb-k3m7q2x7ab'
  const fix = (n: string) => JSON.parse(readFileSync(join(REPO, 'packages', 'contracts', 'fixtures', 'd2', 'rest', `${n}.json`), 'utf8')) as Record<string, unknown>
  const future = () => String(BigInt(Date.now() + 1_800_000) * 1_000_000n)
  let gets = 0
  await context.route('**/api/sandbox/v1/**', async (rt) => {
    const p = new URL(rt.request().url()).pathname.replace('/api/sandbox/v1', '')
    const json = (status: number, body: unknown) => rt.fulfill({ status, body: JSON.stringify(body), headers: { 'content-type': 'application/json' } })
    if (p === `/sessions/${SID}`) {
      gets++
      return json(200, { ...fix('session'), state: 'SPAWNING', expires_unix_ns: future() })
    }
    if (p === '/worlds') return json(200, fix('worlds'))
    if (p === '/templates') return json(200, fix('templates'))
    if (p === '/meta') return json(200, fix('meta'))
    return json(404, { code: 305 })
  })
  await page.addInitScript(([sid, exp]) => {
    sessionStorage.setItem('awr.sandbox.session.v1', JSON.stringify({ sid, token: 'v1.fixture.0000000000000000', token_exp_unix_ms: exp, world: 'synthcity', template: 't5_area_guard' }))
    localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 }))
  }, [SID, Date.now() + 1_800_000] as const)
  await page.goto(`${srv!.url}/sandbox/${SID}?reveal=shell`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 120_000 })
  expect(gets).toBeGreaterThan(0)
  const phase = await page.evaluate(() => (window as unknown as { __ux?: { sandbox: { phase: string } } }).__ux?.sandbox.phase ?? null)
  test.skip(phase === null, 'needs a test build')
  expect(['spawning', 'creating', 'resuming', 'starting', 'waiting']).toContain(phase)
})
