// GC pauses (M16-FR-059; D1-AC-30; PERF-AC-029; AWR-18 §6.3 item 7): CDP tracing with the v8.gc categories during
// flight60 scene=full; GC pause total over the frame time total <= 1 %.
// A pause is GC time on the page's renderer main thread (CrRendererMain): the union of the GC trace events there, so the
// nested phase events (V8.GC_SCAVENGER and its V8.GC_SCAVENGER_SCAVENGE_* children, MinorGC around them) count once,
// and the parallel helpers (ThreadPoolForegroundWorker) and the realtime Worker, which do not stop the frame, do not
// count (FX2-R2: the previous sum over every thread and every nesting level reported 0.94 % where the main thread paused
// 0.16 %). The all-thread sum is still written to metrics.json as gc_all_threads_pct (diagnostic, not judged).
import { runFlight60 } from './fixtures/flight'
import { caseParams, expect, test } from './fixtures/perf'

interface TraceEvent { name: string; ph: string; ts?: number; dur?: number; cat?: string; pid?: number; tid?: number; args?: { name?: string } }

const GC_RE = /GC|Scavenge|MarkCompact|MinorMS/i

/** union length (µs) of [ts, ts + dur) intervals */
function unionUs(iv: [number, number][]): number {
  iv.sort((a, b) => a[0] - b[0])
  let total = 0
  let cur: [number, number] | null = null
  for (const [a, z] of iv) {
    if (!cur || a > cur[1]) {
      if (cur) total += cur[1] - cur[0]
      cur = [a, z]
    } else if (z > cur[1]) cur[1] = z
  }
  if (cur) total += cur[1] - cur[0]
  return total
}

test('GC share during flight60 scene=full', async ({ perfPage }) => {
  test.setTimeout(240_000)
  const cdp = await perfPage.page.context().newCDPSession(perfPage.page)
  const events: TraceEvent[] = []
  cdp.on('Tracing.dataCollected', (d) => { events.push(...(d.value as unknown as TraceEvent[])) })
  const done = new Promise<void>((ok) => cdp.on('Tracing.tracingComplete', () => ok()))
  await cdp.send('Tracing.start', { categories: 'disabled-by-default-v8.gc,v8,__metadata', transferMode: 'ReportEvents' })
  const snap = await runFlight60(perfPage, { city: 'shenzhen', scene: 'full', ...caseParams() })
  await cdp.send('Tracing.end')
  await done
  const mainThreads = new Set(events.filter((e) => e.ph === 'M' && e.name === 'thread_name' && e.args?.name === 'CrRendererMain').map((e) => `${e.pid}:${e.tid}`))
  const gc = events.filter((e) => e.ph === 'X' && GC_RE.test(e.name) && (e.cat ?? '').includes('gc'))
  const allUs = gc.reduce((a, e) => a + (e.dur ?? 0), 0)
  const mainUs = unionUs(gc.filter((e) => mainThreads.has(`${e.pid}:${e.tid}`)).map((e) => [e.ts ?? 0, (e.ts ?? 0) + (e.dur ?? 0)]))
  const frameMs = ((snap.frame as { interval: number[] }).interval ?? []).reduce((a, b) => a + b, 0)
  const pct = frameMs > 0 && mainThreads.size > 0 ? (100 * mainUs) / 1000 / frameMs : Number.NaN
  const allPct = frameMs > 0 ? (100 * allUs) / 1000 / frameMs : Number.NaN
  perfPage.writeMetrics({ gc_pct: pct, gc_all_threads_pct: allPct })
  expect(mainThreads.size, 'renderer main thread named in the trace').toBeGreaterThan(0)
  expect(pct).toBeLessThanOrEqual(1)
})
