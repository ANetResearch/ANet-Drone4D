// Front-end fleet ladder (M16-FR-053; D1-AC-09a n = 200, D1-AC-09b n = 1000; AWR-18 §8.7(1)): flight60 scene=full&n=N
// over the ladder-shenzhen scenario (profile n<N>); the harness starts sampling after the ladder.steady mark.
import { runFlight60 } from './fixtures/flight'
import { caseParams, expect, test } from './fixtures/perf'

test('ladder flight60 scene=full&n=N', async ({ perfPage }) => {
  test.setTimeout(240_000)
  const p = { city: 'shenzhen', scene: 'full' as const, n: 200, ...caseParams() }
  const snap = await runFlight60(perfPage, p)
  const net = snap.net as { swarmHz: number }
  expect(net.swarmHz, 'swarm channel must be live during the ladder run').toBeGreaterThan(5)
})
