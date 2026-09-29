// One flight60 run (M16 §6.8; AWR-18 §3.3, §8.6(4)(5)): binding check, open /world/<city>?bench=flight60&scene=&source=
// [&n=][&chrome=0], reveal, 1000 ms warm-up (g02 §8), reset + mark, wait for bench.done, snapshot. Shared by flight60,
// ladder, net, soak and the paired UI overhead spec.
import { type CaseParams, type PerfPage } from './perf'

export interface FlightOptions {
  /** extra query parameters (rt=1, quality=1, fixedB, perfInject ...) */
  query?: Record<string, string | number>
  /** hook called right after flight.start (net W3 cut, storm injection ...) */
  onStart?: () => Promise<void>
  snapshotName?: string
  timeoutMs?: number
}

export function flightQuery(p: CaseParams, extra: Record<string, string | number> = {}): string {
  const q = new URLSearchParams({ bench: 'flight60', scene: String(p.scene ?? 'pc'), source: String(p.source ?? 'live') })
  if ((p.scene ?? 'pc') === 'pc') q.set('chrome', '0')
  if (p.n) q.set('n', String(p.n))
  if (p.source === 'fake' && p.n) q.set('fakeN', String(p.n))
  for (const [k, v] of Object.entries(extra)) q.set(k, String(v))
  return q.toString()
}

export async function runFlight60(pp: PerfPage, p: CaseParams, o: FlightOptions = {}): Promise<Record<string, unknown>> {
  const city = String(p.city ?? 'shenzhen')
  pp.assertBinding(city)
  await pp.open(`/world/${city}?${flightQuery(p, o.query)}`)
  await pp.waitReveal()
  await pp.page.waitForTimeout(1000)
  await pp.eval((perf) => {
    perf.reset('all')
    perf.mark('flight.start')
  })
  if (o.onStart) await o.onStart()
  await pp.waitBenchDone(o.timeoutMs ?? 120_000)
  const snap = await pp.saveSnapshot(undefined, o.snapshotName ?? 'snapshot.json')
  pp.assertNoPageErrors()
  return snap
}
