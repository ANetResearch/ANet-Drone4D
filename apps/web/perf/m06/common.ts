// Shared helpers of the M06 Playwright specs (functional runs during development; the performance assertions are
// switched on by M06_PERF=1 under the exclusive lock of the acceptance phase, AWR-18 §3). Owner: M06.
import { expect, type Page } from '@playwright/test'

/** performance thresholds are asserted only when the harness sets M06_PERF=1 (M16 run.mjs under the perf lock) */
export const PERF = process.env.M06_PERF === '1'

export interface Watch { errors: string[]; m06: string[] }

/** page errors and M06 diagnostic errors (M06-E001..E009, shader errors, uncaught type errors) */
export function watch(page: Page): Watch {
  const w: Watch = { errors: [], m06: [] }
  page.on('pageerror', (e) => w.errors.push(`${e.name}: ${e.message}`))
  page.on('console', (m) => {
    const t = m.text()
    if (m.type() === 'error' && /M06-E00[1-9]|TypeError|ReferenceError|RangeError|THREE\.WebGLProgram|Shader Error/.test(t)) w.m06.push(t.slice(0, 400))
  })
  return w
}

/** open a world page and wait for the reveal and 20 rendered frames */
export async function openWorld(page: Page, base: string, query = 'source=fake&fakeN=5', world = 'shenzhen'): Promise<void> {
  await page.goto(`${base}/world/${world}?${query}`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 60_000 })
  await page.waitForFunction(() => {
    const p = (window as unknown as { __perf?: { gpu: { passPlan: number }; frame: { count: number } } }).__perf
    return !!p && p.gpu.passPlan > 0 && p.frame.count > 20
  }, null, { timeout: 60_000 })
}

/** read a dotted path of window.__perf (live object, no snapshot) */
export async function perf<T>(page: Page, path: string): Promise<T> {
  return page.evaluate((p) => p.split('.').reduce<unknown>((o, k) => (o as Record<string, unknown>)?.[k], (window as unknown as { __perf: unknown }).__perf) as never, path)
}

/** wait n animation frames in the page */
export async function frames(page: Page, n: number): Promise<void> {
  await page.evaluate(async (k) => {
    for (let i = 0; i < k; i++) await new Promise((r) => requestAnimationFrame(() => r(null)))
  }, n)
}

/** drawing buffer of the viewport canvas, "WxH" */
export async function canvasBuffer(page: Page): Promise<string> {
  return page.evaluate(() => {
    const c = document.querySelector<HTMLCanvasElement>('[data-viewport] canvas') ?? document.querySelector<HTMLCanvasElement>('canvas')
    return c ? `${c.width}x${c.height}` : 'none'
  })
}

/**
 * largest frame interval (ms) over the next windowMs, measured with rAF timestamps in the page (the 1 s window after
 * each warm-up step of M06-AC-008)
 */
export async function maxGapMs(page: Page, windowMs = 1000): Promise<number> {
  return page.evaluate(async (ms) => {
    let last = -1
    let worst = 0
    const t0 = performance.now()
    await new Promise<void>((done) => {
      const step = (t: number): void => {
        if (last >= 0) worst = Math.max(worst, t - last)
        last = t
        if (t - t0 < ms) requestAnimationFrame(step)
        else done()
      }
      requestAnimationFrame(step)
    })
    return worst
  }, windowMs)
}

export type VpHooks = {
  __vp: {
    setMode(m: string): { ok: boolean; reason?: string }
    camera(): { mode: string; pose: { eye_enu_m: number[]; target_enu_m: number[] } } | null
    select(ids: string[]): void
    roster(): { id: string }[]
    dronePose(id: string): [number, number, number] | null
    project(e: number, n: number, u: number): [number, number] | null
    viewportRect(): { x: number; y: number; w: number; h: number } | null
    drones(): { n: number; heroN: number; lowN: number; glyphs: number; markers: number } | null
    backendInfo(): { tier: string; deviceClass: string; state: string; programs: number } | null
    featMatrix(o: { tier?: 'A' | 'B' | 'S' }): Promise<{ tier: string; tests: Record<string, unknown> }>
    vpSession: { rig: { focusSphere(c: number[], r: number): void; lookAtEnu(eye: number[], target: number[]): void } | null }
  }
}

/** first roster id once FakeSource vehicles are present */
export async function firstVehicle(page: Page): Promise<string> {
  await page.waitForFunction(() => (window as unknown as VpHooks).__vp.roster().length >= 1, null, { timeout: 30_000 })
  await page.waitForFunction(() => {
    const vp = (window as unknown as VpHooks).__vp
    const id = vp.roster()[0]?.id
    return !!id && vp.dronePose(id) !== null
  }, null, { timeout: 30_000 })
  return page.evaluate(() => (window as unknown as VpHooks).__vp.roster()[0].id)
}
