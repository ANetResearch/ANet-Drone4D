// M05-AC-007 (TTFP) and AC-004 at run time: __perf.load.ttfp is recorded once per cold open on the four P0 cities and the
// two P1 cities, together with the first-screen bytes of rule R (AWR-16 §4.11). The <= 1.0 s threshold (median of 3)
// is asserted only under the performance lock (M05_PERF=1, make perf-m05). TTFP excludes the first compile of the point
// program: the shader zoo warms it under the mask, nothing compiles after the reveal, and the wait for the zoo is
// load.warmupWaitMs, excluded from ttfp (FX-WEB1).
import { expect, test } from '@playwright/test'
import { PERF, expectNoErrors, openWorld, perf, m05Server, watch } from './common'

const srv = m05Server(2)
const BYTES: Record<string, number> = { shenzhen: 321_384, newyork: 424_212, shanghai: 901_848, suzhou: 243_924, sanfrancisco: 306_708, chicago: 918_492 }

for (const city of Object.keys(BYTES)) {
  test(`TTFP ${city}`, async ({ browser }) => {
    const runs: number[] = []
    for (let k = 0; k < (PERF ? 3 : 1); k++) {
      const ctx = await browser.newContext()
      const page = await ctx.newPage()
      const w = watch(page)
      await openWorld(page, srv.get().url, city)
      const ttfp = await perf<number>(page, 'load.ttfp')
      expect(Number.isFinite(ttfp) && ttfp > 0).toBe(true)
      expect(await perf<number>(page, 'load.firstScreenBytes')).toBe(BYTES[city])
      // the point program is compiled by the shader zoo under the mask, never by the first point frame (D1-AC-02, AC-25):
      // nothing compiles after the reveal and the wait for the zoo is reported apart and excluded (load.warmupWaitMs, 18 §9.3)
      expect(await perf<number>(page, 'gpu.compiledAfterReveal'), 'programs compiled after the reveal').toBe(0)
      const wait = await perf<number>(page, 'load.warmupWaitMs')
      expect(Number.isFinite(wait) && wait >= 0, `load.warmupWaitMs ${wait}`).toBe(true)
      runs.push(ttfp)
      expectNoErrors(w)
      await ctx.close()
    }
    runs.sort((a, b) => a - b)
    test.info().annotations.push({ type: 'ttfp_ms', description: runs.map((x) => x.toFixed(0)).join(', ') })
    if (PERF) expect(runs[1]).toBeLessThanOrEqual(1000)
  })
}
