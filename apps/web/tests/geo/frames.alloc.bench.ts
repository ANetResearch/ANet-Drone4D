// Zero-allocation check of the frames.ts `*Into` functions (M02-AC-012, M02-NFR-005; AWR-03 §3.6 rule 1) plus their
// latency. Performance part: matches only `benchmark.include`, never the unit project; run under the performance
// protocol: `npx vitest bench --run tests/geo/frames.alloc.bench.ts` (Node started with --expose-gc gives the most stable
// heap numbers: `NODE_OPTIONS=--expose-gc`). Budget: heap growth < 4 KB over 10^4 calls of each function.
import { expect, it } from 'vitest'
import {
  ecefToLlaInto,
  enuToNedInto,
  enuToThreeInto,
  fluToFrdInto,
  headingDeg,
  llaToEcefInto,
  llaToWorldInto,
  makeAnchor,
  quatEnuFluFromNedFrdInto,
  quatEnuToThreeInto,
  type Sim3,
  sim3InterpolateInto,
  threeToEnuInto,
  ueCmToWorldInto,
  ueRotToQuatInto,
  worldToLlaInto,
} from '@/engine/geo/frames'

const N = 10_000
const HEAP_BUDGET_BYTES = 4 * 1024
const anchor = makeAnchor({ latDeg: 22.5160584, lonDeg: 113.9432472, hEllipsoidM: 12.2 })!
const out = new Float64Array(8)
const a: Sim3 = { s: 1.2, q: [0, 0, 0.3826834323650898, 0.9238795325112867], t: [1, 2, 3] }
const b: Sim3 = { s: 0.8, q: [0.1, 0.2, 0.3, 0.9273618495495703], t: [-4, 5, 6] }

const CASES: Array<[string, (i: number) => void]> = [
  ['llaToEcefInto', (i) => llaToEcefInto(out, 22.5 + i * 1e-7, 113.9, 10)],
  ['ecefToLlaInto', (i) => ecefToLlaInto(out, -2392316.698 + i, 5387586.971, 2427304.455)],
  ['worldToLlaInto', (i) => worldToLlaInto(out, 100 + i * 0.01, -250, 42, anchor)],
  ['llaToWorldInto', (i) => llaToWorldInto(out, 22.516 + i * 1e-8, 113.943, 30, anchor)],
  ['enuToNedInto', (i) => enuToNedInto(out, i, 2, 3)],
  ['fluToFrdInto', (i) => fluToFrdInto(out, i, 2, 3)],
  ['quatEnuFluFromNedFrdInto', (i) => quatEnuFluFromNedFrdInto(out, 0.9, 0.1, 0.2, 0.3 + i * 1e-9)],
  ['enuToThreeInto', (i) => enuToThreeInto(out, i, 2, 3)],
  ['threeToEnuInto', (i) => threeToEnuInto(out, i, 2, 3)],
  ['quatEnuToThreeInto', (i) => quatEnuToThreeInto(out, 0.1, 0.2, 0.3, 0.9 + i * 1e-9)],
  ['ueCmToWorldInto', (i) => ueCmToWorldInto(out, i, 200, 300, 1, 2, 3)],
  ['ueRotToQuatInto', (i) => ueRotToQuatInto(out, 10, 0, (i % 360) - 180)],
  ['sim3InterpolateInto', (i) => sim3InterpolateInto(out, a, b, (i % 100) / 100)],
  ['headingDeg', (i) => void headingDeg(i * 1e-3)],
]

const gc = (globalThis as { gc?: () => void }).gc

function heapGrowth(fn: (i: number) => void): number {
  for (let i = 0; i < 2000; i++) fn(i) // warm up: JIT and inline caches settle before measuring
  gc?.()
  const before = process.memoryUsage().heapUsed
  for (let i = 0; i < N; i++) fn(i)
  const after = process.memoryUsage().heapUsed
  return after - before
}

for (const [name, fn] of CASES) {
  it(`${name}: heap growth < 4 KB over 10^4 calls`, async ({ bench }) => {
    expect(heapGrowth(fn)).toBeLessThan(HEAP_BUDGET_BYTES)
    let i = 0
    const res = await bench(name, () => fn(i++)).run()
    expect(res.latency.p99).toBeLessThanOrEqual(0.05)
  })
}
