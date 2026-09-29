// GC pauses (M16-FR-059; D1-AC-30; PERF-AC-029; AWR-18 §6.3 item 7): CDP tracing with the v8.gc categories during
// flight60 scene=full; GC pause total over the frame time total <= 1 %.
import { runFlight60 } from './fixtures/flight'
import { caseParams, expect, test } from './fixtures/perf'

interface TraceEvent { name: string; ph: string; dur?: number; cat?: string }

test('GC share during flight60 scene=full', async ({ perfPage }) => {
  test.setTimeout(240_000)
  const cdp = await perfPage.page.context().newCDPSession(perfPage.page)
  const events: TraceEvent[] = []
  cdp.on('Tracing.dataCollected', (d) => { events.push(...(d.value as unknown as TraceEvent[])) })
  const done = new Promise<void>((ok) => cdp.on('Tracing.tracingComplete', () => ok()))
  await cdp.send('Tracing.start', { categories: 'disabled-by-default-v8.gc,v8', transferMode: 'ReportEvents' })
  const snap = await runFlight60(perfPage, { city: 'shenzhen', scene: 'full', ...caseParams() })
  await cdp.send('Tracing.end')
  await done
  const gcUs = events.filter((e) => e.ph === 'X' && /GC|Scavenge|MarkCompact|MinorMS/i.test(e.name) && (e.cat ?? '').includes('gc'))
    .reduce((a, e) => a + (e.dur ?? 0), 0)
  const frameMs = ((snap.frame as { interval: number[] }).interval ?? []).reduce((a, b) => a + b, 0)
  const pct = frameMs > 0 ? (100 * gcUs) / 1000 / frameMs : Number.NaN
  perfPage.writeMetrics({ gc_pct: pct })
  expect(pct).toBeLessThanOrEqual(1)
})
