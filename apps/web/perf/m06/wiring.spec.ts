// Viewport wiring of the point-cloud engine (M05-to-M06 items 1, 2 and 9; M06-FR-037, FR-075; FX-WEB1), functional:
//   * Tier B: M05's EDL composite is the P2 material of the running app (not only in the shader zoo): with a rung that
//     has EDL taps it darkens silhouettes against the same frame without EDL, toggling it compiles nothing and the frame
//     keeps the pass plan;
//   * the PerfGovernor holds the point-cloud CAS (services.cas) with the step-7 floor-release knob;
//   * RedArbiter: in the Class colour mode the class-11 hero owns the red until a higher-priority red entity (the
//     selected vehicle) takes it, then the point cloud demotes class 11 (g50) and gets it back when the selection clears.
// Needs a test build (M06_DIST or dist/ built with VITE_AWR_TEST_SWITCHES=1); no timing thresholds.
import { expect, test, type Page } from '@playwright/test'
import { firstVehicle, frames, openWorld, perf, watch } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

type Pc = { __pc: { engine: { edlMaterial: unknown; stats(): { inflight: number; queued: number; pendingUploadPts: number } }; layers: { setEdl(on: boolean): void; setQuality(q: number | 'auto'): void; setColorMode(m: string): void } } }
type Vp = { __vp: { pointCloud(): { edl: boolean; heroDemoted: boolean } | null; governor(): { hasCas: boolean; knobs: { step: number; id: string; level: number }[] }; select(ids: string[]): void
  vpSession: { be: { composite: { quad: { material: unknown } } | null } } } }

async function converge(page: Page): Promise<void> {
  await page.evaluate(async () => {
    const pc = (window as unknown as Pc).__pc
    const t0 = performance.now()
    let since = -1
    while (performance.now() - t0 < 90_000) {
      await new Promise((r) => requestAnimationFrame(r))
      const s = pc.engine.stats()
      if (s.inflight || s.queued || s.pendingUploadPts) since = -1
      else if (since < 0) since = performance.now()
      else if (performance.now() - since > 2000) return
    }
  })
}

/** RGB of the viewport screenshot, decoded in the page (central 80 % of the canvas, away from the ViewCube) */
async function pixels(page: Page): Promise<number[]> {
  const png = (await page.screenshot()).toString('base64')
  return page.evaluate(async (b64) => {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const bmp = await createImageBitmap(new Blob([bytes], { type: 'image/png' }))
    const c = new OffscreenCanvas(bmp.width, bmp.height)
    const g = c.getContext('2d')!
    g.drawImage(bmp, 0, 0)
    const d = g.getImageData(Math.round(0.1 * bmp.width), Math.round(0.2 * bmp.height), Math.round(0.8 * bmp.width), Math.round(0.75 * bmp.height)).data
    const out: number[] = []
    for (let i = 0; i < d.length; i += 4) out.push(d[i] + d[i + 1] + d[i + 2])
    return out
  }, png)
}

test('Tier B: the M05 EDL composite is the running P2 material and darkens silhouettes', async ({ page }) => {
  test.setTimeout(240_000)
  const w = watch(page)
  await openWorld(page, srv!.url, 'source=fake&fakeN=2&tier=B&fixedB=150000&chrome=0')
  // rung 2 (minimum) has 4 EDL taps (the software auto rungs soft-min and soft have none, g02 §7.2)
  await page.evaluate(() => (window as unknown as Pc).__pc.layers.setQuality(2))
  await page.evaluate(() => (window as unknown as { __vp: { vpSession: { rig: { lookAtEnu(e: number[], t: number[], tr: boolean): void } } } }).__vp.vpSession.rig.lookAtEnu([-250, -350, 160], [0, 0, 40], false))
  await converge(page)
  const wiring = await page.evaluate(() => {
    const vp = (window as unknown as Vp).__vp
    const pc = (window as unknown as Pc).__pc
    return { info: vp.pointCloud(), p2IsEdl: vp.vpSession.be.composite !== null && vp.vpSession.be.composite.quad.material === pc.engine.edlMaterial }
  })
  expect(wiring.info?.edl).toBe(true)
  expect(wiring.p2IsEdl, 'P2 quad renders with the M05 EDL material').toBe(true)
  const programs0 = await perf<number>(page, 'gpu.programs')
  const on = await pixels(page)
  await page.evaluate(() => (window as unknown as Pc).__pc.layers.setEdl(false))
  await frames(page, 6)
  const off = await pixels(page)
  await page.evaluate(() => (window as unknown as Pc).__pc.layers.setEdl(true))
  await frames(page, 6)
  let darker = 0
  let lighter = 0
  let sumOn = 0
  let sumOff = 0
  for (let i = 0; i < on.length; i++) {
    sumOn += on[i]
    sumOff += off[i]
    if (on[i] < off[i] - 24) darker++
    else if (on[i] > off[i] + 24) lighter++
  }
  test.info().annotations.push({ type: 'edl', description: `darker ${darker}, lighter ${lighter} of ${on.length} px; mean ${(sumOn / on.length).toFixed(1)} vs ${(sumOff / on.length).toFixed(1)}` })
  expect(darker, 'EDL darkens depth edges').toBeGreaterThan(on.length * 0.005)
  expect(lighter, 'EDL only darkens').toBeLessThan(darker * 0.05 + 50)
  expect(sumOn).toBeLessThan(sumOff)
  expect(await perf<number>(page, 'gpu.programs'), 'toggling EDL compiles nothing').toBe(programs0)
  expect(await perf<number>(page, 'gpu.planMismatches')).toBe(0)
  expect(await perf<number>(page, 'gpu.compiledAfterReveal')).toBe(0)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})

test('PerfGovernor holds the point-cloud CAS; the class-11 hero yields the red to the selected vehicle', async ({ page }) => {
  test.setTimeout(180_000)
  const w = watch(page)
  await openWorld(page, srv!.url, 'source=fake&fakeN=3')
  const g = await page.evaluate(() => (window as unknown as Vp).__vp.governor())
  expect(g.hasCas).toBe(true)
  expect(g.knobs.map((k) => k.step)).toContain(7)
  expect(g.knobs.find((k) => k.step === 7)?.id).toBe('pc.floor')
  // Class colour mode: class 11 (power lines) is the data hero while nothing else claims the red
  await page.evaluate(() => (window as unknown as Pc).__pc.layers.setColorMode('class'))
  await page.waitForFunction(() => (window as unknown as Vp).__vp.pointCloud()?.heroDemoted === false, null, { timeout: 10_000 })
  const id = await firstVehicle(page)
  await page.evaluate((i) => (window as unknown as Vp).__vp.select([i]), id)
  await page.waitForFunction(() => (window as unknown as Vp).__vp.pointCloud()?.heroDemoted === true, null, { timeout: 10_000 })
  await page.evaluate(() => (window as unknown as Vp).__vp.select([]))
  await page.waitForFunction(() => (window as unknown as Vp).__vp.pointCloud()?.heroDemoted === false, null, { timeout: 10_000 })
  expect(await perf<number>(page, 'gpu.compiledAfterReveal')).toBe(0)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})
