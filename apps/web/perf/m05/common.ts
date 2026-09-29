// Shared helpers of the M05 Playwright specs: server lifecycle, page watch (page errors, long tasks), world opening and
// window.__perf / window.__pc access. Specs need a test build (VITE_AWR_TEST_SWITCHES=1 npm run build) for window.__pc;
// on a production build they skip. Functional smoke by default; performance thresholds (TTFP <= 1 s, frame pacing,
// CAS counts) are asserted only with M05_PERF=1 under the exclusive performance lock (make perf-m05, AWR-18 §3).
import { expect, test, type Page } from '@playwright/test'
import { distReady, startM05Server, worldsReady, type M05Server } from './server'

export const PERF = process.env.M05_PERF === '1'
const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0

/** one server per spec file; k separates the files' ports */
export function m05Server(k: number): { get(): M05Server } {
  let srv: M05Server | null = null
  test.beforeAll(async () => {
    test.skip(!distReady() || !worldsReady(), 'needs apps/web/dist (test build) and the generated worlds')
    srv = await startM05Server(4300 + 10 * OFFSET + k)
  })
  test.afterAll(async () => {
    await srv?.close()
  })
  return { get: () => srv! }
}

export interface Watch { errors: string[] }
export function watch(page: Page): Watch {
  const w: Watch = { errors: [] }
  page.on('pageerror', (e) => w.errors.push(`${e.name}: ${e.message}`))
  return w
}

/** open /world/<id>, wait for the test hook and the first committed frame of the world */
export async function openWorld(page: Page, base: string, world: string, query = ''): Promise<void> {
  await page.goto(`${base}/world/${world}${query}`)
  const hook = await page.waitForFunction(() => 'undefined' !== typeof (window as unknown as { __pc?: unknown }).__pc, null, { timeout: 30_000 }).catch(() => null)
  test.skip(hook === null, 'window.__pc missing: build with VITE_AWR_TEST_SWITCHES=1')
  await waitStreaming(page, world)
}

export async function waitStreaming(page: Page, world: string, timeout = 60_000): Promise<void> {
  await page.waitForFunction((w) => {
    const pc = (window as unknown as { __pc: { engine: { enginePhase: string; info: { worldId: string } | null } } }).__pc
    return pc.engine.info?.worldId === w && pc.engine.enginePhase === 'streaming'
  }, world, { timeout })
}

export async function perf<T>(page: Page, f: string): Promise<T> {
  return page.evaluate((path) => path.split('.').reduce<unknown>((o, k) => (o as Record<string, unknown>)?.[k], (window as unknown as { __perf: unknown }).__perf), f) as Promise<T>
}

export async function stats(page: Page): Promise<Record<string, number | string | boolean>> {
  return page.evaluate(() => (window as unknown as { __pc: { stats(): Record<string, number | string | boolean> } }).__pc.stats())
}

/** move the camera through the viewport session (test builds); ENU eye and target */
export async function look(page: Page, eye: number[], target: number[]): Promise<void> {
  await page.evaluate(([e, t]) => {
    const vp = (window as unknown as { __vp?: { vpSession?: { camera?: { position: { set(x: number, y: number, z: number): void }; lookAt(x: number, y: number, z: number): void;
      updateMatrixWorld(f: boolean): void }; rig?: { setLookAt?(...a: unknown[]): void } } } }).__vp
    const cam = vp?.vpSession?.camera
    if (!cam) return
    const rig = vp?.vpSession?.rig as unknown as { lookAtEnu?(e: number[], t: number[], tr: boolean): void;
      controls?: { setLookAt(a: number, b: number, c: number, d: number, e: number, f: number, g: boolean): void } } | undefined
    if (rig?.lookAtEnu) rig.lookAtEnu(e, t, false)
    else if (rig?.controls?.setLookAt) rig.controls.setLookAt(e[0], e[2], -e[1], t[0], t[2], -t[1], false)
    else {
      cam.position.set(e[0], e[2], -e[1])
      cam.lookAt(t[0], t[2], -t[1])
      cam.updateMatrixWorld(true)
    }
  }, [eye, target])
}

export const frames = (page: Page, n: number): Promise<void> => page.evaluate((k) => new Promise<void>((r) => {
  let i = 0
  const step = (): void => (++i >= k ? r() : void requestAnimationFrame(step))
  requestAnimationFrame(step)
}), n)

export function expectNoErrors(w: Watch): void {
  expect(w.errors, w.errors.join('\n')).toEqual([])
}
