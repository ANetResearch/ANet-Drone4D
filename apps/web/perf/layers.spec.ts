// Layer pairing (M16-FR-059; D1-AC-03b fixed layers, D1-AC-09a diagnosis; AWR-18 §5.2): test build,
// /world/shenzhen?bench=layers&fixedB=25000&n=200 over the ladder-shenzhen n200 scenario (18 §5.2 item 4: drones at the
// ladder 200 load; FX2-R3, S1 had only two vehicles); M06 renders base and base + group into the drawing buffer before the
// frame's own render (1-pixel read-back, finishForBench, ADR-064) and publishes __perf.bench.pairs; done when every group
// has >= 150 pairs. DOM layers (labels, HUD) use static camera blocks and are measured by ui-overhead.
import { caseParams, expect, test } from './fixtures/perf'

test('layer pairing', async ({ perfPage }) => {
  test.setTimeout(600_000)
  const p = caseParams()
  await perfPage.open(`/world/shenzhen?bench=layers&fixedB=${Number(p.fixedB ?? 25000)}${p.n ? `&n=${Number(p.n)}` : ''}`)
  await perfPage.waitReveal()
  await perfPage.waitBenchDone(480_000)
  const snap = await perfPage.saveSnapshot()
  const pairs = ((snap.bench as { pairs?: Record<string, { n: number; medianMs: number }> }).pairs) ?? {}
  for (const g of ['drones', 'trails', 'environment', 'groundSky']) expect(pairs[g]?.n ?? 0, g).toBeGreaterThanOrEqual(150)
  expect((snap.forced as { fixedB?: number } | null)?.fixedB).toBe(Number(p.fixedB ?? 25000))
  perfPage.assertNoPageErrors()
})
