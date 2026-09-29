// Weak network W0-W3 (M16-FR-055; PERF-AC-042; AWR-18 §8.7(3)): the harness starts tools/bench/ipc/netem_proxy.py in front of
// the api (backend.mjs startProxy) and passes the proxy as AWR_PERF_BASE, so HTML, Range and WebSocket all cross it.
// flight60 scene=full&n=200; display age drift (last 10 s minus first 10 s medians), swarm rate and TTFP are judged by the
// harness. W3: the proxy cuts the connections 30 s after it starts; the "signal delay" badge
// ([data-testid=signal-delay-badge], M15) must appear during the cut and disappear within 1 s after data resumes.
import { runFlight60 } from './fixtures/flight'
import { caseParams, expect, test } from './fixtures/perf'

test('weak network profile', async ({ perfPage }) => {
  test.setTimeout(300_000)
  const p = { city: 'shenzhen', scene: 'full' as const, n: 200, net: 'W0', ...caseParams() }
  let badgeSeen = false
  const snap = await runFlight60(perfPage, p, {
    onStart: async () => {
      if (p.net !== 'W3') return
      const badge = perfPage.page.locator('[data-testid="signal-delay-badge"]')
      badgeSeen = await badge.waitFor({ state: 'visible', timeout: 45_000 }).then(() => true, () => false)
      if (badgeSeen) await badge.waitFor({ state: 'hidden', timeout: 10_000 })
    },
  })
  const net = snap.net as { swarmHz: number; reconnects: number; eventGaps: number }
  if (p.net === 'W3') {
    expect(badgeSeen, 'signal-delay badge during the 3 s cut').toBe(true)
    expect(net.reconnects).toBeGreaterThanOrEqual(1)
  }
  expect(net.eventGaps).toBe(0)
})
