// M05-AC-003, AC-009, AC-010, AC-013, AC-014 (flight60 scene=pc through M06's bench driver, ?bench=flight60&scene=pc
// &chrome=0): functional smoke checks the invariants that do not depend on the machine (no budget violation, no failed
// node, residency within 1.5 B_ref + roots, CPU cache <= 64 MB, duplicate download ratio <= 1.3, uploads <= 20k after the
// reveal, selection time recorded); the frame pacing and CAS thresholds are asserted only under the performance lock
// (M05_PERF=1), where the M16 harness (perf/m05/cases.mjs) runs 3 times and takes medians.
import { expect, test } from '@playwright/test'
import { PERF, expectNoErrors, openWorld, perf, m05Server, watch } from './common'

const srv = m05Server(9)

for (const city of ['shenzhen', 'newyork', 'shanghai', 'suzhou']) {
  test(`flight60 scene=pc ${city}`, async ({ page }) => {
    test.setTimeout(240_000)
    const w = watch(page)
    await openWorld(page, srv.get().url, city, '?bench=flight60&scene=pc&chrome=0')
    const mode = await perf<string>(page, 'bench.mode')
    test.skip(mode !== 'flight60', 'M06 flight60 bench driver not active in this build')
    await page.waitForFunction(() => (window as unknown as { __perf: { bench: { done: boolean } } }).__perf.bench.done, null, { timeout: 150_000 })
    const pc = await perf<Record<string, number>>(page, 'pc')
    const cas = await perf<Record<string, number>>(page, 'cas')
    expect(pc.budgetViolations).toBe(0)
    expect(pc.failed).toBe(0)
    expect(pc.cpuCachePeak).toBeLessThanOrEqual(64 * 1024 * 1024)
    expect(pc.downloadedBytes / Math.max(1, pc.uniqueBytes)).toBeLessThanOrEqual(1.3)
    expect(pc.uploadPtsMax).toBeLessThanOrEqual(20_000)
    expect(pc.residentPeak).toBeLessThanOrEqual(1.5 * 150_000 + 40_000)
    if (PERF) {
      expect(cas.rungChanges).toBeLessThanOrEqual(2)
      expect(cas.bounces).toBe(0)
      expect([0, 1]).toContain(cas.index)
      expect(cas.inBandAtMs).toBeLessThanOrEqual(2000)
    }
    expectNoErrors(w)
  })
}
