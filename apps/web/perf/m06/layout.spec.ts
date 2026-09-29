// D1-AC-24 / M06-AC-049, M06-NFR-006, M06-FR-071 (PERF-AC-027; ADR-028, ADR-029): Ctrl+B pressed 10 times and the Dock
// separator dragged 5 times leave the drawing buffer and __perf.gpu.rtAllocs unchanged (the UI floats over the canvas,
// the unobscured rect only moves the projection centre); on a window resize the canvas follows through the R3F
// debounce and every drawing-buffer change allocates the Tier B cloud render target exactly once (no allocation
// without a buffer change). The share of > 50 ms frames during the
// interaction versus the baseline is a performance threshold: measured always, asserted only under M06_PERF=1.
import { expect, test, type Page } from '@playwright/test'
import { canvasBuffer, frames, openWorld, PERF, perf, watch } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

/** share of frame intervals > 50 ms in the probe's interval ring since write position `from` */
async function slowShare(page: Page, from: number): Promise<{ n: number; share: number }> {
  return page.evaluate((f) => {
    const r = (window as unknown as { __perf: { frame: { interval: { buf: Float64Array; n: number } } } }).__perf.frame.interval
    const cap = r.buf.length
    let slow = 0
    let n = 0
    for (let i = Math.max(f, r.n - cap); i < r.n; i++) {
      const v = r.buf[i & (cap - 1)]
      if (!(v > 0)) continue
      n++
      if (v > 50) slow++
    }
    return { n, share: n > 0 ? slow / n : 0 }
  }, from)
}

async function drag(page: Page, sel: string, dy: number): Promise<boolean> {
  const h = page.locator(sel).first()
  if ((await h.count()) === 0 || !(await h.isVisible())) return false
  const b = await h.boundingBox()
  if (!b) return false
  const x = b.x + b.width / 2
  const y = b.y + b.height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  for (let k = 1; k <= 8; k++) await page.mouse.move(x, y + (dy * k) / 8)
  await page.mouse.up()
  return true
}

test('Ctrl+B and Dock drags never resize the canvas or allocate render targets (Tier S)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv!.url, 'source=fake&fakeN=5')
  await page.waitForTimeout(1500)
  const buffer0 = await canvasBuffer(page)
  expect(buffer0).toBe('640x360') // Tier S: DPR 0.5 of 1280 x 720 (M06-AC-048)
  const allocs0 = await perf<number>(page, 'gpu.rtAllocs')
  const f0 = await perf<number>(page, 'frame.interval.n')
  await page.waitForTimeout(2000)
  const base = await slowShare(page, f0)
  const f1 = await perf<number>(page, 'frame.interval.n')
  await page.mouse.click(640, 30)
  for (let i = 0; i < 10; i++) {
    await page.keyboard.press('Control+KeyB')
    await page.waitForTimeout(150)
    expect(await canvasBuffer(page), `Ctrl+B #${i + 1}`).toBe(buffer0)
  }
  // open the Dock (backquote) when it is closed, then drag its separator 5 times
  const dock = page.locator('[data-slot="rail-host"][data-side="bottom"]')
  if ((await dock.count()) > 0 && (await dock.getAttribute('data-state')) !== 'open') await page.keyboard.press('Backquote')
  await page.waitForTimeout(500)
  let dragged = 0
  for (let i = 0; i < 5; i++) {
    if (await drag(page, '[data-slot="rail-host"][data-side="bottom"] [data-slot="resizable-handle"]', i % 2 === 0 ? -40 : 40)) dragged++
    await page.waitForTimeout(300)
    expect(await canvasBuffer(page), `Dock drag #${i + 1}`).toBe(buffer0)
  }
  test.info().annotations.push({ type: 'dock drags', description: String(dragged) })
  await frames(page, 10)
  expect(await canvasBuffer(page)).toBe(buffer0)
  expect(await perf<number>(page, 'gpu.rtAllocs')).toBe(allocs0)
  const during = await slowShare(page, f1)
  test.info().annotations.push({ type: '> 50 ms share', description: `baseline ${(base.share * 100).toFixed(1)} %, interaction ${(during.share * 100).toFixed(1)} %` })
  if (PERF) expect(during.share).toBeLessThanOrEqual(base.share + 0.01)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})

test('window resize: each drawing-buffer change allocates cloudRT exactly once (Tier B)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv!.url, 'source=fake&fakeN=2&tier=B')
  await page.waitForTimeout(1000)
  const allocs0 = await perf<number>(page, 'gpu.rtAllocs')
  const buffer0 = await canvasBuffer(page)
  // page-side record of every rAF: drawing buffer and rtAllocs (the resize steps are driven from Playwright, so their
  // spacing is measured rather than assumed)
  await page.evaluate(() => {
    const rec: [number, string, number][] = []
    ;(window as unknown as { __m06rec: unknown }).__m06rec = rec
    const c = document.querySelector<HTMLCanvasElement>('[data-viewport] canvas') ?? document.querySelector<HTMLCanvasElement>('canvas')
    const tick = (t: number): void => {
      const a = (window as unknown as { __perf: { gpu: { rtAllocs: number } } }).__perf.gpu.rtAllocs
      const b = c ? `${c.width}x${c.height}` : 'none'
      const last = rec[rec.length - 1]
      if (!last || last[1] !== b || last[2] !== a) rec.push([t, b, a])
      if (rec.length < 1000) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
  const stepsAt: number[] = []
  for (const wpx of [1300, 1320, 1340, 1360]) {
    await page.setViewportSize({ width: wpx, height: 720 })
    stepsAt.push(await page.evaluate(() => performance.now()))
    await page.waitForTimeout(30) // inside the 120 ms debounce
  }
  await page.waitForTimeout(600)
  await frames(page, 10)
  const buffer1 = await canvasBuffer(page)
  expect(buffer1).not.toBe(buffer0)
  const rec = await page.evaluate(() => (window as unknown as { __m06rec: [number, string, number][] }).__m06rec)
  test.info().annotations.push({ type: 'resize record', description: JSON.stringify({ allocs0, buffer0, stepsAt, rec }) })
  // every drawing-buffer change allocates cloudRT exactly once and nothing else allocates; the R3F debounce (120 ms) folds
  // steps that reach the page close together (Playwright's viewport calls are slow on SwiftShader, so the number of
  // changes is bounded by the number of steps rather than asserted to be one)
  let changes = 0
  for (let i = 1; i < rec.length; i++) if (rec[i][1] !== rec[i - 1][1]) changes++
  const allocs = (await perf<number>(page, 'gpu.rtAllocs')) - allocs0
  expect(rec[0][2]).toBe(allocs0)
  expect(allocs).toBe(changes)
  expect(allocs).toBeGreaterThanOrEqual(1)
  expect(allocs).toBeLessThanOrEqual(stepsAt.length)
  const rt = await page.evaluate(() => {
    const be = (window as unknown as { __vp: { vpSession: { be: { cloudTarget(): { width: number; height: number } | null } | null } } }).__vp.vpSession.be
    const t = be?.cloudTarget()
    return t ? `${t.width}x${t.height}` : 'none'
  })
  expect(rt).toBe(buffer1)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})
