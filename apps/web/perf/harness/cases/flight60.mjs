// Core flight60 and front-end fleet cases (M16 §6.7.4, §6.8, §6.9; AWR-18 §8.5, §8.6, §8.7(1)).
import { CITIES, cas, firstScreen, framePc, frameFull, m, server, streaming } from './_metrics.mjs'

const S1 = { kind: 'live', world: 'shenzhen', scenario: 's1-shenzhen-facade', scenarioProfile: 'perf' }
const MAIN4 = ['shenzhen', 'newyork', 'shanghai', 'suzhou']
const FULL_RE = ['shenzhen', 'newyork', 'shanghai']                // 18 §8.6(6): all metrics gating
const DUP = ['shenzhen', 'shanghai', 'suzhou']

/** @type {import('../types').CaseDef[]} */
export default [
  ...CITIES.map((city) => {
    const all = FULL_RE.includes(city)
    const p0 = MAIN4.includes(city)
    return {
      id: `flight60.${city}.pc`, kind: 'pw', spec: 'perf/flight60.spec.ts', build: 'production', browser: 'C1',
      backend: { kind: 'live', world: city, scenario: `free-${city}` }, params: { city, scene: 'pc' }, runs: 3, timeoutS: 360,
      acIds: ['D1-AC-03a', 'D1-AC-02', 'D1-AC-04', 'D1-AC-06', 'PERF-AC-002', 'PERF-AC-003'], priority: p0 ? 'P0' : 'P1', layer: 'core',
      gates: ['G2d', 'G3', 'G4'], owner: 'M16',
      // suzhou, sanfrancisco, chicago: TTFP and p95 only (18 §8.6(6)); the rest is recorded
      metrics: all ? [...framePc(), ...firstScreen(), ...cas(), ...streaming(true, DUP.includes(city))]
        : [...framePc(false).map((x) => (x.key === 'frame.p95_ms' ? { ...x, gating: true } : x)), ...firstScreen(),
          ...cas(false), ...streaming(false, DUP.includes(city))],
    }
  }),
  {
    id: 'flight60.shenzhen.full', kind: 'pw', spec: 'perf/flight60.spec.ts', build: 'production', browser: 'C1', backend: S1,
    params: { city: 'shenzhen', scene: 'full' }, runs: 3, timeoutS: 360, acIds: ['D1-AC-03b', 'D1-AC-04', 'D1-AC-06', 'PERF-AC-010'],
    priority: 'P0', layer: 'core', gates: ['G2d', 'G3', 'G4'], owner: 'M16',
    metrics: [...frameFull(), ...firstScreen(), ...cas(), ...streaming(), m('main_js_p50_ms', 'ms', 'main_js_p50_ms'), ...server()],
  },
  {
    id: 'flight60.shenzhen.fake', kind: 'pw', spec: 'perf/flight60.spec.ts', build: 'production', browser: 'C1',
    backend: { kind: 'fake', world: 'shenzhen', fakeN: 2 }, params: { city: 'shenzhen', scene: 'full', source: 'fake' }, runs: 1,
    timeoutS: 300, acIds: ['D1-AC-35'], priority: 'P0', layer: 'core', gates: ['G2d'], owner: 'M16',
    metrics: [...frameFull(false), ...firstScreen(false)],
  },
  {
    id: 'flight60-wx', kind: 'pw', spec: 'perf/flight60.spec.ts', build: 'production', browser: 'C1',
    backend: { ...S1, scenarioProfile: 'wx-storm' }, params: { city: 'shenzhen', scene: 'full', governorOrder: 1 }, runs: 3, timeoutS: 360,
    acIds: ['D1-AC-03b', 'PERF-AC-010'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'], owner: 'M16',
    metrics: [...frameFull(), ...cas(false)],
  },
  ...[10, 50, 100, 200, 500, 1000].map((n) => ({
    id: `ladder.front.n${n}`, kind: 'pw', spec: 'perf/ladder.spec.ts', build: 'production', browser: 'C1',
    backend: { kind: 'live', world: 'shenzhen', scenario: 'ladder-shenzhen', scenarioProfile: `n${n}`, waitMark: 'ladder.steady' },
    params: { city: 'shenzhen', scene: 'full', n }, runs: 3, timeoutS: 420,
    acIds: n === 200 ? ['D1-AC-09a', 'PERF-AC-012'] : n === 1000 ? ['D1-AC-09b', 'PERF-AC-013'] : ['D1-AC-09a'],
    priority: n === 200 ? 'P0' : n === 1000 ? 'P1' : 'P2', layer: n === 1000 ? 'ext' : 'core',
    gates: n === 200 ? ['G2d', 'G3', 'G4'] : ['G2w', 'G3'], owner: 'M16',
    metrics: n === 200 ? [...frameFull(), m('worker_decode_p95_ms', 'ms', 'worker_decode_p95', false, { extra: true }), ...server()]
      : n === 1000 ? [m('frame.p95_ms', 'ms', 'frame_p95_n1000'), m('over100_pct', 'pct', 'over100_n1000'),
        m('worker_decode_p95_ms', 'ms', 'worker_decode_p95', true, { extra: true }), m('frame.mean_ms', 'ms', null), ...server()]
        : [...frameFull(false), m('worker_decode_p95_ms', 'ms', null, false, { extra: true })],
  })),
  {
    id: 'layers', kind: 'pw', spec: 'perf/layers.spec.ts', build: 'test', browser: 'C1', backend: S1,
    params: { city: 'shenzhen', fixedB: 25000 }, runs: 3, timeoutS: 600, acIds: ['D1-AC-03b', 'PERF-AC-010'], priority: 'P0', layer: 'core',
    gates: ['G2w', 'G3', 'G4'], owner: 'M16',
    metrics: [...['drones', 'trails', 'environment', 'groundSky'].map((g) => m(`layer.${g}_ms`, 'ms', `layer_${g}_ms`, true, { extract: `layer_${g}_ms` })),
      m('layers_fixed_total_ms', 'ms', 'layers_fixed_total_ms', true, { extra: true })],
  },
  {
    id: 'quality', kind: 'pw', spec: 'perf/quality.spec.ts', build: 'test', browser: 'C1',
    backend: { kind: 'live', world: 'shenzhen', scenario: 'free-shenzhen' }, params: { city: 'shenzhen', scene: 'pc', quality: 1 }, runs: 3,
    timeoutS: 420, acIds: ['D1-AC-05', 'PERF-AC-005'], priority: 'P1', layer: 'ext', gates: ['G2w', 'G3', 'G4'], owner: 'M16',
    metrics: [m('hole_rate_pct', 'pct', 'hole_rate_pct', true, { source: 'script' }), m('points_avg', 'count', 'points_avg')],
  },
  {
    id: 'governor', kind: 'pw', spec: 'perf/governor.spec.ts', build: 'test', browser: 'C1',
    backend: { kind: 'live', world: 'shenzhen', scenario: 'ladder-shenzhen', scenarioProfile: 'n200', waitMark: 'ladder.steady' },
    params: { city: 'shenzhen', scene: 'full', n: 200, perfInject: 'busyMs:40' }, runs: 3, timeoutS: 420,
    acIds: ['PERF-AC-011'], priority: 'P0', layer: 'core', gates: ['G2w', 'G3', 'G4'], owner: 'M16', metrics: [],
  },
]
