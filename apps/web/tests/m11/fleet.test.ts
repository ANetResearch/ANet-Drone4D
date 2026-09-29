// stores/fleet.ts (M11-FR-097, M11 §6.4.15, M11-AC-046 functional part): one O(N) pass writes the typed rows and the
// summary (counts per FlightState and owner, red-highlight alerts, lowest known battery, in-air and unknown-battery
// counts, staleness) and writes the store only when the swarm, the roster, the link state or the staleness changed;
// id, model and row lookups go through the roster; N = 1000 end to end from a FakeSource client.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { FlightFlags, FlightState } from '@awr/contracts/enums'
import { packCtrl, packFs } from '@awr/contracts/layouts'
import { createFakeRtClient, type RtClientImpl } from '@/net/rt/client'
import type { ConnState, RosterEntry, RosterView, SwarmSnapshot } from '@/net/rt/types'
import { FLEET_STALE_MS, fleetIdOf, fleetModelOf, fleetRowOf, fleetRowOfId, fleetRows, fleetStore, resetFleetSummary, summariseFleet, type FleetSource } from '@/stores/fleet'

function stub(n: number): FleetSource & { swarm: SwarmSnapshot; status: ConnState; roster: RosterView & { version: number } } {
  const cap = 1024
  const swarm: SwarmSnapshot = { n, seq: 1, tSimMs: 1234, version: 1, agentNo: new Uint16Array(cap), fs: new Uint8Array(cap), battery: new Uint8Array(cap),
    flags: new Uint8Array(cap), ctrl: new Uint8Array(cap), pos: new Float32Array(3 * cap), vel: new Float32Array(3 * cap), quat: new Float32Array(4 * cap) }
  const entries: RosterEntry[] = []
  for (let i = 0; i < n; i++) {
    swarm.agentNo[i] = 10 + i
    entries.push({ agentNo: 10 + i, id: `uav${i}`, model: i % 2 ? 'x500' : 'p600', kind: 'uav', producer: 'sim-core', simulated: true, lifecycle: 'READY' })
  }
  const roster = {
    version: 1,
    get size() {
      return entries.length
    },
    get: (no: number) => entries.find((e) => e.agentNo === no),
    idOf: (no: number) => entries.find((e) => e.agentNo === no)?.id,
    agentNoOf: (id: string) => entries.find((e) => e.id === id)?.agentNo ?? -1,
    entries: () => entries,
  }
  return { swarm, roster, status: 'LIVE' }
}

let clients: RtClientImpl[] = []
beforeEach(() => resetFleetSummary())
afterEach(() => {
  for (const c of clients) c.close()
  clients = []
})

describe('fleet summary', () => {
  it('counts, alerts, battery and rows', () => {
    const s = stub(6)
    const sw = s.swarm
    const air = FlightFlags.ARMED | FlightFlags.IN_AIR | FlightFlags.LOC_OK
    const rows: [number, number, number, number][] = [ // fs, flags, owner, battery
      [FlightState.FLYING, air, 1, 80],
      [FlightState.FLYING, air | FlightFlags.ALERT, 2, 55], // ALERT flag -> red
      [FlightState.CORRECTING, air, 2, 40], // CORRECTING..CRASHED -> red
      [FlightState.LANDED, FlightFlags.LOC_OK, 0, 255], // LANDED is not red; battery unknown
      [FlightState.READY, FlightFlags.LOC_OK, 0, 12],
      [FlightState.CRASHED, 0, 5, 0],
    ]
    rows.forEach(([fs, fl, own, bat], i) => {
      sw.fs[i] = packFs(fs, 3)
      sw.flags[i] = fl
      sw.ctrl[i] = packCtrl(own, false, 2, 1)
      sw.battery[i] = bat
    })
    expect(summariseFleet(s, 1000)).toBe(true)
    const st = fleetStore.getState()
    expect(st.n).toBe(6)
    expect(st.alerts).toBe(3)
    expect(st.minBatteryPct).toBe(0)
    expect(st.batteryUnknown).toBe(1)
    expect(st.inAir).toBe(3)
    expect(st.stale).toBe(false)
    expect(st.tSimMs).toBe(1234)
    expect(Array.from(st.byState.subarray(0, 14))).toEqual([0, 0, 0, 1, 0, 2, 1, 0, 0, 0, 0, 0, 1, 1])
    expect(Array.from(st.byOwner)).toEqual([2, 1, 2, 0, 0, 1, 0, 0])
    expect(Array.from(fleetRows.alert.subarray(0, 6))).toEqual([0, 1, 1, 0, 0, 1])
    expect(Array.from(fleetRows.sub.subarray(0, 6))).toEqual([3, 3, 3, 3, 3, 3])
    expect(fleetRows.flags[1]).toBe(air | FlightFlags.ALERT)
    expect(fleetIdOf(2, s)).toBe('uav2')
    expect(fleetModelOf(1, s)).toBe('x500')
    expect(fleetRowOf(13)).toBe(3)
    expect(fleetRowOf(99)).toBe(-1)
    expect(fleetRowOfId('uav5', s)).toBe(5)
  })

  it('writes only on change: swarm version, roster version, link state, staleness', () => {
    const s = stub(3)
    expect(summariseFleet(s, 1000)).toBe(true)
    const v0 = fleetStore.getState().version
    expect(summariseFleet(s, 1100)).toBe(false)
    s.swarm.version++
    expect(summariseFleet(s, 1200)).toBe(true)
    s.roster.version++
    expect(summariseFleet(s, 1300)).toBe(true)
    s.status = 'DEGRADED'
    expect(summariseFleet(s, 1400)).toBe(true)
    expect(fleetStore.getState().stale).toBe(true)
    expect(fleetRows.stale[0]).toBe(1)
    s.status = 'LIVE'
    expect(summariseFleet(s, 1500)).toBe(true)
    expect(fleetStore.getState().stale).toBe(false)
    // no new swarm sample for longer than the stale threshold
    expect(summariseFleet(s, 1500 + FLEET_STALE_MS + 1)).toBe(true)
    expect(fleetStore.getState().stale).toBe(true)
    expect(fleetStore.getState().version).toBe(v0 + 5)
  })

  it('shrinking fleets clear the old row lookups', () => {
    const s = stub(5)
    summariseFleet(s, 0)
    expect(fleetRowOf(14)).toBe(4)
    s.swarm.n = 2
    s.swarm.version++
    summariseFleet(s, 1)
    expect(fleetRows.n).toBe(2)
    expect(fleetRowOf(14)).toBe(-1)
    expect(fleetRowOf(11)).toBe(1)
  })

  it('N = 1000 from a FakeSource client', async () => {
    const c = createFakeRtClient({ n: 1000 })
    clients.push(c)
    c.subscribe('swarm/state', { rate: 10 })
    c.subscribe('fleet/roster', { rate: 10 })
    const end = performance.now() + 5000
    while (performance.now() < end && !(c.status === 'LIVE' && c.swarm.n === 1000 && c.roster.size === 1000)) {
      c.swapFrame()
      await new Promise((r) => setTimeout(r, 16))
    }
    expect(summariseFleet(c, performance.now())).toBe(true)
    const st = fleetStore.getState()
    expect(st.n).toBe(1000)
    expect(st.byState[FlightState.FLYING]).toBe(1000)
    expect(st.byOwner[2]).toBe(1000) // MISSION
    expect(fleetIdOf(999, c)).toBe('uav1000')
    expect(fleetModelOf(0, c)).toBe('p600')
  })
})
