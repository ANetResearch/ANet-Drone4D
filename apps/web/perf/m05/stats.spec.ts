// M05-AC-031 (FR-051, FR-052): stores/world is written at most 4 times per second with every HUD field; __perf.pc and
// __perf.cas carry the M05 fields of AWR-18 §9.2 (including the 1.x additions); limitedBy uses the 0-4 encoding.
import { expect, test } from '@playwright/test'
import { expectNoErrors, openWorld, perf, m05Server, watch } from './common'

const srv = m05Server(5)

test('world store cadence and __perf fields (M05-AC-031)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen')
  const writes = await page.evaluate(async () => {
    const pc = (window as unknown as { __pc: { world(): Record<string, unknown> } }).__pc
    let last = pc.world()
    let n = 0
    const t0 = performance.now()
    while (performance.now() - t0 < 3000) {
      await new Promise((r) => requestAnimationFrame(r))
      const cur = pc.world()
      if (cur !== last) n++
      last = cur
    }
    return n
  })
  expect(writes).toBeLessThanOrEqual(13) // <= 4 Hz over 3 s (+1 for phase edges)
  const world = await page.evaluate(() => (window as unknown as { __pc: { world(): Record<string, unknown> } }).__pc.world())
  for (const k of ['worldId', 'phase', 'progress', 'firstScreenBytes', 'rung', 'B', 'Beff', 'Bfloor', 'lo', 'hi', 'drawn', 'limitedBy', 'clampedByCapacity', 'floorHeld', 'inflight', 'failed']) {
    expect(world, k).toHaveProperty(k)
  }
  expect(world.Bfloor).toBe(20_000)
  const pc = await perf<Record<string, unknown>>(page, 'pc')
  for (const k of ['drawn', 'limitedBy', 'budgetViolations', 'residentPts', 'residentPeak', 'cpuCacheBytes', 'downloadedBytes', 'uniqueBytes', 'progress', 'poolRows',
    'fillRate', 'maxPxEff', 'rsEff', 'poolStalls', 'pageUtil']) expect(pc, k).toHaveProperty(k)
  expect(pc.poolRows).toBe(62)
  expect(pc.budgetViolations).toBe(0)
  const cas = await perf<Record<string, unknown>>(page, 'cas')
  expect(cas.B_floor).toBe(20_000)
  expect(cas.index).toBe(0)
  expectNoErrors(w)
})
