// Node microbenchmarks of engine/time (M12 §5.3 `interp_bench.mjs` migrated; NFR-001, NFR-002, NFR-004). Performance
// part, run under the performance protocol only (Vitest 5 benchmark API, test context `bench`; the file matches only the
// `benchmark.include` pattern, never the unit project): `npx vitest bench --run tests/time/interp.bench.ts`.
// sampleSwarm N = 1000 p95 <= 0.3 ms and N = 200 <= 0.1 ms (Node 22 prototype: 0.142 / 0.027 ms); the clock phase
// p99 <= 0.02 ms; the column aggregation of 500k markers at 1920 px <= 1.5 ms.
import { expect, it } from 'vitest'
import { InterpRing, MarkerClass, SimClockView, TrackModel, newDronePoseSoA } from '@/engine/time/index'

function ring(n: number): InterpRing {
  const r = new InterpRing(n)
  for (let a = 0; a < n; a++) {
    const s = r.slotFor(a)
    for (let k = 0; k < 32; k++) {
      const t = k * 100
      r.push(s, t, a + 0.012 * t, -a, 50 + Math.sin(t / 700), 12, 0, 0, 0, 0, Math.sin(t / 900), Math.cos(t / 900), 0)
    }
  }
  r.eMaxMs = 300
  return r
}

for (const [n, budget] of [[1000, 0.3], [200, 0.1]] as const) {
  it(`sampleSwarm N = ${n}: p95 <= ${budget} ms`, async ({ bench }) => {
    const r = ring(n)
    const out = newDronePoseSoA(n)
    let t = 2.0
    const res = await bench(`sampleSwarm ${n}`, () => {
      t = t >= 3.0 ? 2.0 : t + 0.016
      r.sampleSwarm(t, out)
    }).run()
    expect(res.latency.p99).toBeLessThanOrEqual(budget * 2)
  })
}

it('clock phase p99 <= 0.02 ms', async ({ bench }) => {
  const c = new SimClockView()
  c.onTimeFields(1, 1, 1, 0, 0, 0)
  let now = 0
  const res = await bench('clock tick', () => {
    now += 16.7
    if (now % 100 < 16.7) c.delay.onSample(now)
    c.tick(now, now)
  }).run()
  expect(res.latency.p99).toBeLessThanOrEqual(0.02)
})

it('timeline columns: 500k markers at 1920 px <= 1.5 ms', async ({ bench }) => {
  const m = new TrackModel()
  for (let i = 0; i < 500_000; i++) m.addMarker(i * 7.2, i % 50 === 0 ? 2 : 0, i % 50 === 0 ? MarkerClass.WARNING : MarkerClass.LIFECYCLE, 0xffff, i)
  let k = 0
  const res = await bench('columns', () => {
    k++
    m.columns(k * 1e-6, 3600 + k * 1e-6, 1920)
  }).run()
  expect(res.latency.p99).toBeLessThanOrEqual(1.5)
})
