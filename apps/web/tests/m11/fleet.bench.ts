// M11-AC-046 (performance part, run under the performance protocol only): one fleet summary pass over N = 1000 rows must
// stay <= 1 ms on Tier S (M11 §6.4.15). Vitest 5 benchmark API (test context `bench`); the file matches only the
// `benchmark.include` pattern, never the unit project: `npx vitest bench --run tests/m11/fleet.bench.ts`.
import { expect, it } from 'vitest'
import { packCtrl, packFs } from '@awr/contracts/layouts'
import type { ConnState, RosterEntry, SwarmSnapshot } from '@/net/rt/types'
import { resetFleetSummary, summariseFleet, type FleetSource } from '@/stores/fleet'

const N = 1000
const cap = 1024
const swarm: SwarmSnapshot = { n: N, seq: 1, tSimMs: 0, version: 1, agentNo: new Uint16Array(cap), fs: new Uint8Array(cap), battery: new Uint8Array(cap),
  flags: new Uint8Array(cap), ctrl: new Uint8Array(cap), pos: new Float32Array(3 * cap), vel: new Float32Array(3 * cap), quat: new Float32Array(4 * cap) }
const entries: RosterEntry[] = []
for (let i = 0; i < N; i++) {
  swarm.agentNo[i] = i
  swarm.fs[i] = packFs(5 + (i % 3))
  swarm.flags[i] = 0x37
  swarm.ctrl[i] = packCtrl(i % 8, false, 2, 1)
  swarm.battery[i] = i % 101
  entries.push({ agentNo: i, id: `uav${i}`, model: 'p600', kind: 'uav', producer: 'sim-core', simulated: true, lifecycle: 'READY' })
}
const src: FleetSource = {
  swarm,
  status: 'LIVE' as ConnState,
  roster: { version: 1, size: N, get: (no) => entries[no], idOf: (no) => entries[no]?.id, agentNoOf: (id) => Number(id.slice(3)), entries: () => entries },
}

it('fleet summary N = 1000: p99 <= 1 ms', async ({ bench }) => {
  resetFleetSummary()
  let t = 0
  const r = await bench('summariseFleet', () => {
    swarm.version++
    t += 250
    summariseFleet(src, t)
  }).run()
  expect(r.latency.p99).toBeLessThanOrEqual(1)
})
