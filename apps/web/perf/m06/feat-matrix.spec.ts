// D1-AC-14 / M06-AC-001, AC-002, AC-016 (PERF-AC-014; g01 §3, §9): the 28-item feature matrix + PointPool of the
// regression page equal the classic-path expectations of g01 §3 item by item on Tier B and Tier S (?tier=B, ?tier=S
// test switch), __perf.forced.tier records the switch, and every rendered frame issues exactly the pass plan
// (render.calls == passPlan). Tier A (?tier=A&allowFallback=1, C2 flags) is P1 and not implemented (M06 report).
// Functional case: no timing assertion. Registered for the M16 harness in perf/m06/cases.mjs.
import { expect, test } from '@playwright/test'
import { diffMatrix } from './featMatrix.expected'
import { frames, openWorld, perf, watch, type VpHooks } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

for (const tier of ['S', 'B'] as const) {
  test(`feature matrix and pass plan on Tier ${tier}`, async ({ page }) => {
    const w = watch(page)
    await openWorld(page, srv!.url, `source=fake&fakeN=5&tier=${tier}`)
    const info = await page.evaluate(() => (window as unknown as VpHooks).__vp.backendInfo())
    expect(info).toMatchObject({ tier, deviceClass: 'software', state: 'READY' })
    expect(await perf<string>(page, 'meta.tier')).toBe(tier)
    expect(await perf<{ tier?: string } | null>(page, 'forced')).toMatchObject({ tier })
    // pass plan on 30 consecutive frames (FakeSource vehicles moving, labels, trails)
    const plan = await page.evaluate(async () => {
      const p = (window as unknown as { __perf: { gpu: { calls: number; passPlan: number; planMismatches: number } } }).__perf
      const out: [number, number][] = []
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => requestAnimationFrame(() => r(null)))
        out.push([p.gpu.calls, p.gpu.passPlan])
      }
      return { out, mismatches: p.gpu.planMismatches }
    })
    for (const [calls, want] of plan.out) expect(calls).toBe(want)
    expect(plan.mismatches).toBe(0)
    // the regression page: 28 items + PointPool on this tier's backend path (own renderer, own canvas)
    const m = await page.evaluate((t) => (window as unknown as VpHooks).__vp.featMatrix({ tier: t }), tier)
    expect(m.tier).toBe(tier)
    expect(Object.keys(m.tests).length).toBe(29)
    expect(diffMatrix(m.tests)).toEqual([])
    await frames(page, 10)
    expect(w.errors).toEqual([])
    expect(w.m06).toEqual([])
  })
}
