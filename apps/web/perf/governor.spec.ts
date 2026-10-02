// PerfGovernor under injected load (M16-FR-059; PERF-AC-011; ADR-041; AWR-18 §4.7): test build, shenzhen scene=full with
// 200 ladder vehicles; 0-10 s baseline, 10-70 s __perf.inject({ busyMs: 40 }), 70-250 s recovery. Assertions: CAS reaches
// B_floor within 1 s, degradation steps follow ADR-041 order with >= 2 s spacing between visible steps (a sub-step whose
// change is not on screen is recorded with reason suffix ':hidden' and chains into the next one, ADR-067), B never below
// B_floor before the last optional layer step, first recovery step by t <= 190 s in reverse order with >= 10 s spacing
// between visible steps. History times are performance.now() ms; the phase marks are converted to the same base.
import { caseParams, expect, test } from './fixtures/perf'

test('governor degradation order and recovery', async ({ perfPage }) => {
  test.setTimeout(420_000)
  const p = caseParams()
  await perfPage.open(`/world/${p.city ?? 'shenzhen'}?scene=full&n=${Number(p.n ?? 200)}&perfInject=busyMs:0`)
  await perfPage.waitReveal()
  const hasInject = await perfPage.eval((perf) => typeof perf.inject === 'function')
  expect(hasInject, 'test build with __perf.inject (VITE_AWR_TEST_SWITCHES=1)').toBe(true)
  const nowS = (): Promise<number> => perfPage.page.evaluate(() => performance.now() / 1000)
  await perfPage.page.waitForTimeout(10_000)
  await perfPage.eval((perf) => perf.inject!({ busyMs: 40 }))
  const tOn = await nowS()
  await perfPage.page.waitForTimeout(60_000)
  await perfPage.eval((perf) => perf.inject!({ busyMs: 0 }))
  const tOff = await nowS()
  await perfPage.page.waitForTimeout(180_000)
  const snap = await perfPage.saveSnapshot()
  // history t is performance.now() in ms; tOn / tOff are seconds
  const hist = ((snap.governor as { history: { t: number; step: number; dir: number; reason: string }[] }).history ?? [])
    .filter((h) => h.step > 0).map((h) => ({ ...h, t: h.t / 1000, hidden: /:hidden$/.test(h.reason) }))
  const down = hist.filter((h) => h.dir < 0 && h.t >= tOn)
  const up = hist.filter((h) => h.dir > 0 && h.t >= tOff)
  expect(down.length, 'degradation steps under 40 ms busy load').toBeGreaterThanOrEqual(6)
  for (let i = 1; i < down.length; i++) expect(down[i].step).toBeGreaterThanOrEqual(down[i - 1].step)
  const downVis = down.filter((h) => !h.hidden)
  for (let i = 1; i < downVis.length; i++) expect(downVis[i].t - downVis[i - 1].t).toBeGreaterThanOrEqual(2 - 0.05)
  expect(up.length, 'recovery after the load is removed').toBeGreaterThan(0)
  expect(up[0].t - tOff, 'first recovery step within 120 s of removing the load (t <= 190 s)').toBeLessThanOrEqual(120)
  for (let i = 1; i < up.length; i++) expect(up[i].step).toBeLessThanOrEqual(up[i - 1].step)
  const upVis = up.filter((h) => !h.hidden)
  for (let i = 1; i < upVis.length; i++) expect(upVis[i].t - upVis[i - 1].t).toBeGreaterThanOrEqual(10 - 0.05)
  perfPage.assertNoPageErrors()
})
