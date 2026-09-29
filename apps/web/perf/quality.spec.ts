// Image quality sampling (M16-FR-059; D1-AC-05; AWR-18 §4.5): test build, scene=pc with ?quality=1; 12 coverage masks in
// __perf.quality.samples are compared with the full reference render of M05 (apps/web/dev/oracles/fullref.html,
// window.__fullref.render(poses) and holeRate(ref, test)). The hole rate (12-frame mean) goes to metrics.json.
import { runFlight60 } from './fixtures/flight'
import { caseParams, expect, test } from './fixtures/perf'

test('quality samples and hole rate', async ({ perfPage, browser }) => {
  test.setTimeout(420_000)
  const p = { city: 'shenzhen', scene: 'pc' as const, ...caseParams() }
  const snap = await runFlight60(perfPage, p, { query: { quality: 1 } })
  const samples = ((snap.quality as { samples?: { t: number; pose: number[]; mask: number[] | null }[] })?.samples) ?? []
  expect(samples.length, 'twelve quality samples').toBe(12)
  const ref = await browser.newPage()
  const r = await ref.goto(`${perfPage.base}/dev/oracles/fullref.html?world=${p.city}`)
  if (!r || r.status() !== 200) throw new Error('fullref oracle page is not served by this build (M05 dev page); PERF-AC-005 needs it')
  const holes = await ref.evaluate(async (s) => {
    const w = window as unknown as { __fullref: { render(poses: number[][]): Promise<number[][]>; holeRate(a: number[], b: number[]): number } }
    const refs = await w.__fullref.render(s.map((x) => x.pose))
    return s.map((x, i) => (x.mask ? w.__fullref.holeRate(refs[i], x.mask) : null))
  }, samples)
  const valid = holes.filter((h): h is number => typeof h === 'number')
  expect(valid.length).toBe(12)
  perfPage.writeMetrics({ hole_rate_pct: (100 * valid.reduce((a, b) => a + b, 0)) / valid.length })
})
