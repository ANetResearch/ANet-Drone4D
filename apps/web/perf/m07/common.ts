// Shared helpers of the M07 Playwright specs (perf/m07/*.spec.ts). Specs need a test build (VITE_AWR_TEST_SWITCHES=1)
// for window.__env and skip otherwise; functional by default, performance thresholds only with M07_PERF=1 under the
// exclusive performance lock (M16 harness, AWR-18 §3).
import { test, type Page } from '@playwright/test'
import { distReady, startM07Server, worldsReady, type M07Server } from './server'

export const PERF = process.env.M07_PERF === '1'
const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0

export function m07Server(k: number): { get(): M07Server } {
  let srv: M07Server | null = null
  test.beforeAll(async () => {
    test.skip(!distReady() || !worldsReady(), 'needs apps/web/dist (test build) and the generated worlds')
    srv = await startM07Server(4400 + 10 * OFFSET + k)
  })
  test.afterAll(async () => {
    await srv?.close()
  })
  return { get: () => srv! }
}

/** open a world on FakeSource and wait for the environment hook and the first keyframe */
export async function openEnv(page: Page, base: string, query = ''): Promise<void> {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`${e.name}: ${e.message}`))
  await page.goto(`${base}/world/shenzhen?source=fake&envPeriodS=0${query}`)
  const hook = await page.waitForFunction(() => typeof (window as unknown as { __env?: unknown }).__env !== 'undefined', null, { timeout: 60_000 }).catch(() => null)
  test.skip(hook === null, 'window.__env missing: build with VITE_AWR_TEST_SWITCHES=1')
  await page.waitForFunction(() => (window as unknown as { __env: { state(): { state: string } } }).__env.state().state !== 'EMPTY', null, { timeout: 60_000 })
  ;(page as unknown as { __errors: string[] }).__errors = errors
}

export function pageErrors(page: Page): string[] {
  return (page as unknown as { __errors?: string[] }).__errors ?? []
}

export interface EnvState {
  state: string; version: number; quality: string; mor: number; rainK: number; drawCount: number; tRenderNs: number; scalars: number[]
  perf: { draws: number; verts: number; live: { rain: number; snow: number; dust: number; arrows: number } }
}

export const envState = (page: Page): Promise<EnvState> =>
  page.evaluate(() => (window as unknown as { __env: { state(): EnvState } }).__env.state())

export const programs = (page: Page): Promise<number> =>
  page.evaluate(() => (window as unknown as { __perf: { gpu: { programs: number } } }).__perf.gpu.programs)
