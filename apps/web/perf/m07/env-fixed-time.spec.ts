// env-fixed-time (M07-NFR-019, M07-AC-021 functional part; M12 fixed render time): test builds pin the render time with
// ?simTime=<s>&paused=1 (engine/time/register.ts; window.__time.setFixed moves it). Two pages inject the same step
// keyframe (heavy rain at an absolute time with zero anchors, __env.injectStepAt), hide the vehicles and their labels, lock the point
// budget (?fixedB, so CAS cannot pick a different selection) and look from the same pose:
//   page A is pinned directly at T; page B starts at T0 < T and is played forward to T in 0.2 s steps.
// The canvases must agree (mean absolute difference per channel <= 1/255, max <= 4/255 over the central region; the
// corners carry the ViewCube and possible toasts) and page A must not change while wall time passes (a pinned instant).
import { expect, test, type Page } from '@playwright/test'
import { m07Server, openEnv, pageErrors } from './common'

const srv = m07Server(4)
const T0 = 30
const T = 36
const POSE: [number[], number[]] = [[-420, -560, 140], [0, 0, 60]]

async function prepare(page: Page, t: number): Promise<void> {
  await openEnv(page, srv.get().url, `&tier=S&chrome=0&fixedB=25000&simTime=${t}&paused=1`)
  // the world open cuts the camera to the world's home pose (pc.world.opened): look only after it
  await page.waitForFunction(() => {
    const pc = (window as unknown as { __pc?: { engine: { enginePhase: string; info: { worldId: string } | null } } }).__pc
    return pc?.engine.info?.worldId === 'shenzhen' && pc.engine.enginePhase === 'streaming'
  }, null, { timeout: 60_000 })
  await page.evaluate(() => {
    const w = window as unknown as { __pc: { layers: { setVisible(id: string, v: boolean): void } } }
    w.__pc.layers.setVisible('drones', false)
    w.__pc.layers.setVisible('trails', false)
    // the vehicles' labels too: a stale vehicle keeps its DOM label ("STALE n s") until the stale timeout, so a page that
    // settles early showed it in the first capture only (FX2-R2: the faster Tier S frames moved the capture earlier)
    w.__pc.layers.setVisible('labels', false)
  })
  const v = await page.evaluate((t0) => (window as unknown as { __env: { injectStepAt(id: string, s: number): number } }).__env.injectStepAt('heavyRain', t0 - 10), T0)
  expect(v).toBeGreaterThan(0)
  await page.waitForFunction((ver) => (window as unknown as { __env: { state(): { version: number } } }).__env.state().version === ver, v, { timeout: 30_000 })
  await page.evaluate(([e, tg]) => (window as unknown as { __vp: { vpSession: { rig: { lookAtEnu(a: number[], b: number[], c: boolean): void } } } }).__vp.vpSession.rig.lookAtEnu(e, tg, false), POSE)
}

async function settle(page: Page): Promise<void> {
  await page.evaluate(async () => {
    const pc = (window as unknown as { __pc: { engine: { stats(): { inflight: number; queued: number; pendingUploadPts: number } } } }).__pc
    const t0 = performance.now()
    let since = -1
    while (performance.now() - t0 < 60_000) {
      await new Promise((r) => requestAnimationFrame(r))
      const s = pc.engine.stats()
      if (s.inflight || s.queued || s.pendingUploadPts) since = -1
      else if (since < 0) since = performance.now()
      else if (performance.now() - since > 2500) return
    }
  })
}

/** RGBA of the central region of a page screenshot, decoded in that page */
async function pixels(page: Page): Promise<number[]> {
  const png = (await page.screenshot()).toString('base64')
  return page.evaluate(async (b64) => {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const bmp = await createImageBitmap(new Blob([bytes], { type: 'image/png' }))
    const c = new OffscreenCanvas(bmp.width, bmp.height)
    const g = c.getContext('2d')!
    g.drawImage(bmp, 0, 0)
    const x0 = Math.round(0.1 * bmp.width)
    const y0 = Math.round(0.15 * bmp.height)
    return Array.from(g.getImageData(x0, y0, Math.round(0.75 * bmp.width), Math.round(0.7 * bmp.height)).data)
  }, png)
}

function compare(a: number[], b: number[]): { mean: number; max: number } {
  let sum = 0
  let max = 0
  let n = 0
  for (let i = 0; i < a.length; i++) {
    if ((i & 3) === 3) continue
    const d = Math.abs(a[i] - b[i])
    sum += d
    n++
    if (d > max) max = d
  }
  return { mean: sum / n, max }
}

test('a pinned instant renders the same whether reached directly or by playing forward (M07-AC-021)', async ({ browser }) => {
  test.setTimeout(300_000)
  const ctxA = await browser.newContext({ viewport: { width: 1280, height: 720 } })
  const ctxB = await browser.newContext({ viewport: { width: 1280, height: 720 } })
  const A = await ctxA.newPage()
  const B = await ctxB.newPage()
  await prepare(A, T)
  await prepare(B, T0)
  // page B plays forward from T0 to T in 0.2 s steps (one rendered frame per step)
  await B.evaluate(async ([t0, t1]) => {
    const tm = (window as unknown as { __time: { setFixed(t: number | null): void } }).__time
    for (let t = t0; t <= t1 + 1e-9; t += 0.2) {
      tm.setFixed(Math.min(t, t1))
      await new Promise((r) => requestAnimationFrame(r))
    }
    tm.setFixed(t1)
  }, [T0, T])
  await settle(A)
  await settle(B)
  // the paused source leaves its vehicles stale; their glyphs leave the view at the stale timeout (labels are hidden
  // above): capture once none is drawn on either page, so both captures see the same vehicle-free scene
  for (const p of [A, B]) {
    await p.waitForFunction(() => ((window as unknown as { __vp: { drones(): { glyphs: number; markers: number } | null } }).__vp.drones()?.glyphs ?? 0) === 0,
      null, { timeout: 60_000 }).catch(() => {})
  }
  const a1 = await pixels(A)
  await A.waitForTimeout(2000)
  const a2 = await pixels(A)
  const b = await pixels(B)
  const still = compare(a1, a2)
  const fwd = compare(a1, b)
  const msg = `still mean ${still.mean.toFixed(3)} max ${still.max}; forward mean ${fwd.mean.toFixed(3)} max ${fwd.max}`
  test.info().annotations.push({ type: 'diff', description: msg })
  console.log(`env-fixed-time ${msg}`)
  expect(still.max, 'a pinned instant does not change with wall time').toBeLessThanOrEqual(4)
  expect(fwd.mean, 'forward playback to T equals the direct instant (mean)').toBeLessThanOrEqual(1)
  expect(fwd.max, 'forward playback to T equals the direct instant (max)').toBeLessThanOrEqual(4)
  const s = await A.evaluate(() => (window as unknown as { __env: { state(): { tRenderNs: number } } }).__env.state().tRenderNs)
  expect(Math.abs(s / 1e9 - T)).toBeLessThan(1e-6)
  expect(pageErrors(A)).toEqual([])
  expect(pageErrors(B)).toEqual([])
  await ctxA.close()
  await ctxB.close()
})
