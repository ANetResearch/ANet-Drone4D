// M05-AC-008 (FR-007): in-app world switches shenzhen -> newyork -> shanghai -> suzhou through the router: each switch
// records load.switchMs, keeps the program count and the render-target allocations, and leaves only the new world in
// GPU residency and in the CPU cache. The 1.5 s bound is asserted under the performance lock only.
import { expect, test } from '@playwright/test'
import { PERF, expectNoErrors, openWorld, perf, stats, m05Server, waitStreaming, watch } from './common'

const srv = m05Server(3)

test('world switches keep programs and RTs, release the old world (M05-AC-008)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen')
  await page.waitForTimeout(1500)
  const programs = await perf<number>(page, 'gpu.programs')
  const rts = await perf<number>(page, 'gpu.rtAllocs')
  for (const city of ['newyork', 'shanghai', 'suzhou']) {
    await page.evaluate((c) => {
      history.pushState(null, '', `/world/${c}`)
      dispatchEvent(new PopStateEvent('popstate'))
    }, city)
    await waitStreaming(page, city)
    const switchMs = await perf<number>(page, 'load.switchMs')
    expect(Number.isFinite(switchMs)).toBe(true)
    if (PERF) expect(switchMs).toBeLessThanOrEqual(1500)
    const s = await stats(page)
    const resident = await page.evaluate(() => {
      const t = (window as unknown as { __pc: { engine: { store: { N: number; poolBase: Int32Array; numPoints: Int32Array } } } }).__pc.engine.store
      let r = 0
      for (let i = 0; i < t.N; i++) if (t.poolBase[i] >= 0) r += t.numPoints[i]
      return r
    })
    expect(s.residentPts).toBe(resident)
    await page.waitForTimeout(500)
    expect(await perf<number>(page, 'gpu.programs')).toBe(programs)
    expect(await perf<number>(page, 'gpu.rtAllocs')).toBe(rts)
  }
  expectNoErrors(w)
})
