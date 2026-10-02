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
    // M06's bench driver is part of both builds (AWR-18 §9.5): the run must be a flight60 run, never skipped (FX-WEB1)
    expect(await perf<string>(page, 'bench.mode')).toBe('flight60')
    await page.waitForFunction(() => (window as unknown as { __perf: { bench: { done: boolean } } }).__perf.bench.done, null, { timeout: 150_000 })
    // the camera followed the flight to its end and the frame ring carries flight times (steady window t in (2, 60] s)
    expect(await perf<number>(page, 'bench.flightT')).toBeGreaterThanOrEqual(60)
    const ts = await page.evaluate(() => {
      const r = (window as unknown as { __perf: { frame: { t: { buf: Float64Array; n: number } } } }).__perf.frame.t
      let steady = 0
      for (let i = Math.max(0, r.n - r.buf.length); i < r.n; i++) if (r.buf[i & (r.buf.length - 1)] > 2) steady++
      return steady
    })
    expect(ts, 'frames inside the steady window').toBeGreaterThan(30)
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
