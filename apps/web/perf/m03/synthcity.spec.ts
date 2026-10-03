// Generated demo city synthcity (DEMO-W; ADR-077; M03-AC-036): functional smoke of progressive loading and of the density
// controller (CAS) on Tier S and Tier B. Opens /world/synthcity on the M05 static server (apps/web/dist or M05_DIST, a
// private VITE_AWR_TEST_SWITCHES=1 build) and samples window.__pc every 250 ms for 12 s from the first committed frame.
//   * Tier S: the first frame stays within the Tier S first-screen cap (1e5 points, AWR-16 §4.11 rule R), resident points
//     then grow and the drawn count rises above the first frame (progressive loading); the rung stays on the 7-rung ladder
//     and the budget never falls under its floor (CAS); no request fails.
//   * Tier B on this machine's SwiftShader: the frame time leaves no headroom, so the CAS must hold the lowest rung and the
//     first screen (the same series as shenzhen, ADR-077 report); the PerfGovernor may lower the budget below the CAS floor.
//   * Tier B with a locked budget (?fixedB=600000, CAS bypassed): the Tier B pipeline streams beyond the first screen.
// Thresholds of time (TTFP, convergence) belong to the perf cases, not to this smoke. Screenshots go to
// runs/playwright/synthcity-<case>.png (README material; the world is free to redistribute).
import { expect, test } from '@playwright/test'
import { existsSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { expectNoErrors, openWorld, watch } from '../m05/common'
import { distReady, startM05Server, type M05Server } from '../m05/server'

const ROOT = resolve(import.meta.dirname, '../../../..')
const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0
const FIRST_CAP = { S: 100_000, B: 450_000 } as const
let srv: M05Server | null = null

test.beforeAll(async () => {
  test.skip(!distReady() || !existsSync(join(ROOT, 'worlds', 'synthcity', 'world.json')),
    'needs apps/web/dist (test build) and worlds/synthcity (make demo-world)')
  srv = await startM05Server(4300 + 10 * OFFSET + 31)
})
test.afterAll(async () => {
  await srv?.close()
})

interface Sample { t: number; drawn: number; B: number; Bfloor: number; rung: number; rungName: string; limitedBy: string; resident: number; failed: number }

async function sample(page: import('@playwright/test').Page): Promise<Sample[]> {
  return page.evaluate(async () => {
    const pc = (window as unknown as { __pc: { stats(): Record<string, unknown> } }).__pc
    const out: Sample[] = []
    const t0 = performance.now()
    while (performance.now() - t0 < 12_000) {
      const s = pc.stats()
      out.push({ t: Math.round(performance.now() - t0), drawn: Number(s.drawn), B: Number(s.B), Bfloor: Number(s.Bfloor), rung: Number(s.rungIndex),
        rungName: String(s.rungName), limitedBy: String(s.limitedBy), resident: Number(s.residentPts), failed: Number(s.failed) })
      await new Promise((r) => setTimeout(r, 250))
    }
    return out
  })
}

function note(name: string, series: Sample[]): void {
  const first = series.find((s) => s.drawn > 0)!
  const last = series[series.length - 1]
  const rungs = [...new Set(series.map((s) => s.rungName))].join(' > ')
  test.info().annotations.push({ type: name, description: `first ${first.drawn} pts; last drawn ${last.drawn}, B ${Math.round(last.B)}, ` +
    `resident ${last.resident}, rungs ${rungs}, limitedBy ${last.limitedBy}` })
}

async function shot(page: import('@playwright/test').Page, name: string): Promise<void> {
  const dir = join(ROOT, 'runs', 'playwright')
  mkdirSync(dir, { recursive: true })
  await page.screenshot({ path: join(dir, `synthcity-${name}.png`) })
}

test('synthcity Tier S: progressive loading and density control', async ({ page }) => {
  test.setTimeout(120_000)
  const w = watch(page)
  await openWorld(page, srv!.url, 'synthcity', '?tier=S')
  const series = await sample(page)
  const first = series.find((s) => s.drawn > 0)
  expect(first, 'no frame drew any point').toBeTruthy()
  expect(first!.drawn).toBeLessThanOrEqual(FIRST_CAP.S)
  expect(series[series.length - 1].resident).toBeGreaterThan(first!.drawn)
  expect(Math.max(...series.map((s) => s.drawn))).toBeGreaterThan(first!.drawn)
  for (const s of series) {
    expect(s.rung).toBeGreaterThanOrEqual(0)
    expect(s.rung).toBeLessThanOrEqual(6)
    expect(s.B).toBeGreaterThanOrEqual(s.Bfloor)
    expect(s.failed).toBe(0)
  }
  note('tierS', series)
  await shot(page, 'tierS')
  expectNoErrors(w)
})

test('synthcity Tier B (SwiftShader): the CAS holds the lowest rung under frame-time pressure', async ({ page }) => {
  test.setTimeout(120_000)
  const w = watch(page)
  await openWorld(page, srv!.url, 'synthcity', '?tier=B')
  const series = await sample(page)
  const first = series.find((s) => s.drawn > 0)
  expect(first, 'no frame drew any point').toBeTruthy()
  expect(first!.drawn).toBeLessThanOrEqual(FIRST_CAP.B)
  for (const s of series) {
    expect(s.rung).toBeGreaterThanOrEqual(0)
    expect(s.rung).toBeLessThanOrEqual(6)
    expect(s.B).toBeGreaterThan(0)
    expect(s.drawn).toBeLessThanOrEqual(Math.max(s.B, first!.drawn))
    expect(s.failed).toBe(0)
  }
  note('tierB', series)
  expectNoErrors(w)
})

test('synthcity Tier B with a locked budget: progressive loading beyond the first screen', async ({ page }) => {
  test.setTimeout(120_000)
  const w = watch(page)
  await openWorld(page, srv!.url, 'synthcity', '?tier=B&fixedB=600000')
  const series = await sample(page)
  const first = series.find((s) => s.drawn > 0)
  expect(first, 'no frame drew any point').toBeTruthy()
  expect(first!.drawn).toBeLessThanOrEqual(FIRST_CAP.B)
  expect(series[series.length - 1].resident).toBeGreaterThan(first!.drawn)
  expect(Math.max(...series.map((s) => s.drawn))).toBeGreaterThan(first!.drawn)
  for (const s of series) expect(s.failed).toBe(0)
  note('tierB-fixedB', series)
  await shot(page, 'tierB')
  expectNoErrors(w)
})
