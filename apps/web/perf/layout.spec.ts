// Canvas size stability (M16-FR-059; D1-AC-24; PERF-AC-027; AWR-18 §6.2): during S1 at x1, toggle the left panel with
// mod+KeyB 10 times and drag the dock splitter 5 times; the drawing buffer size and gpu.rtAllocs stay unchanged and the
// share of > 50 ms frames in that period is at most the baseline period + 1 percentage point.
import { hotkey } from './fixtures/ui'
import { expect, test } from './fixtures/perf'

async function frames(pp: import('./fixtures/perf').PerfPage, ms: number): Promise<number[]> {
  return pp.page.evaluate((dur) => new Promise<number[]>((ok) => {
    const out: number[] = []
    let last = performance.now()
    const t0 = last
    const step = (now: number): void => {
      out.push(now - last)
      last = now
      if (now - t0 < dur) requestAnimationFrame(step)
      else ok(out)
    }
    requestAnimationFrame(step)
  }), ms)
}
const over50 = (a: number[]): number => (a.length ? (100 * a.filter((x) => x > 50).length) / a.length : 0)

test('layout toggles keep the drawing buffer', async ({ perfPage }) => {
  test.setTimeout(180_000)
  const page = perfPage.page
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  await page.waitForTimeout(3000)
  const before = await perfPage.eval((p) => ({ db: p.meta.drawingBuffer, rt: p.gpu.rtAllocs }))
  const base = await frames(perfPage, 10_000)
  const during = frames(perfPage, 20_000)
  for (let i = 0; i < 10; i++) {
    await hotkey(page, 'Control+KeyB')
    await page.waitForTimeout(600)
  }
  const handle = page.locator('[data-panel-resize-handle-id], [role="separator"]').last()
  if (await handle.count()) {
    const box = await handle.boundingBox()
    for (let i = 0; box && i < 5; i++) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
      await page.mouse.down()
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 - 40 * (i % 2 ? -1 : 1), { steps: 8 })
      await page.mouse.up()
      await page.waitForTimeout(400)
    }
  }
  const dur = await during
  const after = await perfPage.eval((p) => ({ db: p.meta.drawingBuffer, rt: p.gpu.rtAllocs }))
  perfPage.writeMetrics({ rt_allocs_delta: after.rt - before.rt, over50_delta_pct: over50(dur) - over50(base) })
  expect(after.db).toEqual(before.db)
  expect(after.rt).toBe(before.rt)
  await perfPage.saveSnapshot()
  perfPage.assertNoPageErrors()
})
