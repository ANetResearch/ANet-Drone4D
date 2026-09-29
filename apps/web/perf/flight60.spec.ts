// flight60 (M16-FR-050; D1-AC-03a/03b, 02, 04, 06; AWR-18 §8.6): scene=pc (six cities, ?chrome=0), scene=full (shenzhen + S1),
// source=fake (front-end isolation, never gating). Thresholds are judged by the harness from snapshot.json
// (perf/harness/analyze.mjs); the spec only drives the flight, checks the binding and collects the snapshot.
import { runFlight60 } from './fixtures/flight'
import { caseParams, expect, test } from './fixtures/perf'

test('flight60', async ({ perfPage }) => {
  test.setTimeout(240_000)
  const p = { city: 'shenzhen', scene: 'pc' as const, ...caseParams() }
  const snap = await runFlight60(perfPage, p)
  const meta = snap.meta as { tier: string; deviceClass: string; scene?: string }
  expect(meta.tier).toBe('S')
  expect((snap.frame as { interval: number[] }).interval.length).toBeGreaterThan(600)   // 60 s at >= 10 fps
})
