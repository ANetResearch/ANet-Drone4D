// React commit budget (M16-FR-059; PERF-AC-020; AWR-18 §6.2, §6.3 item 1): profiling build (vite build --mode perf-profiling),
// flight60 scene=full without user input; __perf.ui.react { commits, durations } from the Profiler wrappers.
import { runFlight60 } from './fixtures/flight'
import { caseParams, expect, test } from './fixtures/perf'

test('steady React commits', async ({ perfPage }) => {
  test.setTimeout(240_000)
  const snap = await runFlight60(perfPage, { city: 'shenzhen', scene: 'full', ...caseParams() })
  const meta = snap.meta as { mode: string }
  expect(meta.mode, 'profiling build').toBe('profiling')
  const r = ((snap.ui as { react?: { commits: number; durations: number[] } }).react) ?? { commits: 0, durations: [] }
  perfPage.writeMetrics({ react_commits_per_s: r.commits / 60 })
  expect(r.commits / 60).toBeLessThanOrEqual(12)
})
