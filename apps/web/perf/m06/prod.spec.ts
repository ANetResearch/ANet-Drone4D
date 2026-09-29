// M06-AC-010 production smoke (ADR-044; AWR-18 §9.1 item 4): a production build (no VITE_AWR_TEST_SWITCHES, given in
// M06_PROD_DIST) ignores the test switches: window.__perf.inject is undefined, ?tier=B opens Tier S on this machine
// with __perf.forced === null, and no test hook (__vp) exists. The bundle scan itself is tests/m06/lint/prod-bundle-scan.mjs
// (make scan-m06-bundle). Skipped when M06_PROD_DIST is not set.
import { resolve } from 'node:path'
import { expect, test } from '@playwright/test'
import { openWorld, perf, watch } from './common'
import { startM06Server, type M06Server } from './server'

const PROD = process.env.M06_PROD_DIST ? resolve(process.env.M06_PROD_DIST) : null
let srv: M06Server | null = null
test.beforeAll(async () => {
  if (PROD) srv = await startM06Server(undefined, PROD)
})
test.afterAll(async () => {
  await srv?.close()
})

test('production build ignores the test switches (M06-AC-010)', async ({ page }) => {
  test.skip(!PROD, 'set M06_PROD_DIST to a production build')
  const w = watch(page)
  await openWorld(page, srv!.url, 'source=fake&fakeN=2&tier=B&perfInject=busyMs:60&selftest=nofix')
  const s = await page.evaluate(() => {
    const g = window as unknown as { __perf: { inject?: unknown; forced: unknown; meta: { tier: string } }; __vp?: unknown }
    return { inject: typeof g.__perf.inject, forced: g.__perf.forced, tier: g.__perf.meta.tier, vp: typeof g.__vp }
  })
  expect(s).toEqual({ inject: 'undefined', forced: null, tier: 'S', vp: 'undefined' })
  // an active busyMs:60 injection would make nearly every frame an over-50 ms frame of ours
  await page.waitForTimeout(2000)
  const [over, frames] = [await perf<number>(page, 'loaf.oursOver50'), await perf<number>(page, 'frame.count')]
  expect(over).toBeLessThan(frames / 2)
  expect(w.errors).toEqual([])
})
