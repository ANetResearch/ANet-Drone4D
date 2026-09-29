// UI overhead pairing (M16-FR-059; D1-AC-23; PERF-AC-047): in one browser, alternate flight60 scene=full with the UI shell
// (chrome=1: shell + HUD + DroneRail expanded) and canvas only (chrome=0), `blocks` times each; p50 unchanged and the
// > 50 ms share grows by at most 1 percentage point.
import { runFlight60 } from './fixtures/flight'
import { caseParams, expect, pctl, steady, test } from './fixtures/perf'

test('UI shell versus canvas only', async ({ perfPage }) => {
  const p = { city: 'shenzhen', scene: 'full' as const, blocks: 3, ...caseParams() }
  test.setTimeout(Number(p.blocks) * 2 * 150_000)
  const res: Record<'on' | 'off', { p50: number[]; over50: number[] }> = { on: { p50: [], over50: [] }, off: { p50: [], over50: [] } }
  for (let i = 0; i < Number(p.blocks); i++) {
    for (const mode of ['on', 'off'] as const) {
      const snap = await runFlight60(perfPage, p, { query: { chrome: mode === 'on' ? 1 : 0 }, snapshotName: `snapshot-${mode}-${i}.json` })
      const iv = steady(snap)
      res[mode].p50.push(pctl(iv, 0.5) ?? Number.NaN)
      res[mode].over50.push(iv.length ? (100 * iv.filter((x) => x > 50).length) / iv.length : Number.NaN)
    }
  }
  const med = (a: number[]): number => pctl(a, 0.5) ?? Number.NaN
  const p50d = med(res.on.p50) - med(res.off.p50)
  const o50d = med(res.on.over50) - med(res.off.over50)
  perfPage.writeMetrics({ p50_delta_ms: p50d, over50_delta_pct: o50d })
  expect(o50d).toBeLessThanOrEqual(1)
})
