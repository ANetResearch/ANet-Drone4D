// D1-AC-25 / M06-AC-008, M06-NFR-004 (PERF-AC-028; ADR-007, g01 §4.3): after the mask is lifted the first use of each
// feature compiles no program: weather preset (M07, when its test hook exists), selecting a drone, Third, FPV (or its
// no_camera_sensor refusal), the P600 hero appearing (procedural stand-in, ?hero=procedural), a click pick, a point
// colour mode switch (M05 __pc hook) and an in-app world switch. gpu.programs stays at gpu.programsAtReveal and
// gpu.compiledAfterReveal stays 0. The 150 ms maximum frame interval within 1 s of each step is a performance
// threshold: measured always, asserted only under M06_PERF=1 (acceptance phase, exclusive lock).
import { expect, test, type Page } from '@playwright/test'
import { firstVehicle, frames, maxGapMs, openWorld, PERF, perf, watch, type VpHooks } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

const GAP_MS = 150

interface Step { name: string; gapMs: number; programs: number }

async function step(page: Page, name: string, run: () => Promise<unknown>, log: Step[]): Promise<void> {
  await run()
  const gapMs = await maxGapMs(page, 1000)
  const programs = await perf<number>(page, 'gpu.programs')
  log.push({ name, gapMs, programs })
}

test('no program is compiled after the reveal (M06-AC-008)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv!.url, 'source=fake&fakeN=5&hero=procedural')
  const atReveal = await perf<number>(page, 'gpu.programsAtReveal')
  expect(atReveal).toBeGreaterThan(0)
  const log: Step[] = []
  const id = await firstVehicle(page)
  await frames(page, 10)

  const hasEnv = await page.evaluate(() => typeof (window as unknown as { __env?: { setPreset?: unknown } }).__env?.setPreset === 'function')
  if (hasEnv) {
    await step(page, 'weather preset', () => page.evaluate(() => (window as unknown as { __env: { setPreset(p: string): void } }).__env.setPreset('overcast')), log)
  }
  await step(page, 'select', () => page.evaluate((i) => (window as unknown as VpHooks).__vp.select([i]), id), log)
  await step(page, 'third', async () => {
    const r = await page.evaluate(() => (window as unknown as VpHooks).__vp.setMode('third'))
    expect(r.ok).toBe(true)
  }, log)
  await step(page, 'fpv', async () => {
    const r = await page.evaluate(() => (window as unknown as VpHooks).__vp.setMode('fpv'))
    expect(r.ok || r.reason === 'no_camera_sensor').toBe(true)
  }, log)
  // P600 hero: orbit 3.5 m from the selected drone (screen radius well above 48 px), re-aimed every 100 ms because
  // FakeSource vehicles keep moving
  await step(page, 'hero', async () => {
    await page.evaluate(() => (window as unknown as VpHooks).__vp.setMode('orbit'))
    const ok = await page.evaluate(async (i) => {
      const vp = (window as unknown as VpHooks).__vp
      const t0 = performance.now()
      while (performance.now() - t0 < 15_000) {
        const p = vp.dronePose(i)
        if (p) vp.vpSession.rig?.lookAtEnu([p[0] + 2, p[1] - 2, p[2] + 2], p)
        if ((vp.drones()?.heroN ?? 0) >= 1) return true
        await new Promise((r) => setTimeout(r, 100))
      }
      return false
    }, id)
    expect(ok).toBe(true)
  }, log)
  await step(page, 'click pick', async () => {
    const at = await page.evaluate(async (i) => {
      const vp = (window as unknown as VpHooks).__vp
      const p0 = vp.dronePose(i)
      if (p0) vp.vpSession.rig?.lookAtEnu([p0[0] + 20, p0[1] - 20, p0[2] + 15], p0)
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(null))))
      const p = vp.dronePose(i)
      const s = p ? vp.project(p[0], p[1], p[2]) : null
      const r = vp.viewportRect()
      return s && r ? [r.x + s[0], r.y + s[1]] : null
    }, id)
    expect(at).not.toBeNull()
    await page.mouse.click(at![0], at![1])
  }, log)
  const hasPc = await page.evaluate(() => typeof (window as unknown as { __pc?: { layers?: { setColorMode?: unknown } } }).__pc?.layers?.setColorMode === 'function')
  if (hasPc) {
    await step(page, 'colour mode', () => page.evaluate(() => (window as unknown as { __pc: { layers: { setColorMode(m: string): void } } }).__pc.layers.setColorMode('height')), log)
  }
  await step(page, 'world switch', async () => {
    await page.evaluate(() => {
      history.pushState(null, '', `/world/newyork${location.search}`)
      dispatchEvent(new PopStateEvent('popstate'))
    })
    await page.waitForFunction(() => location.pathname === '/world/newyork')
    await page.waitForTimeout(3000)
  }, log)

  for (const s of log) expect(s.programs, `programs after "${s.name}"`).toBe(atReveal)
  expect(await perf<number>(page, 'gpu.compiledAfterReveal')).toBe(0)
  const gaps = log.map((s) => `${s.name} ${s.gapMs.toFixed(0)} ms`).join(', ')
  test.info().annotations.push({ type: 'max frame interval within 1 s', description: gaps })
  if (PERF) for (const s of log) expect(s.gapMs, `max frame interval after "${s.name}"`).toBeLessThanOrEqual(GAP_MS)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})
