// Fleet summary and DroneRail rows (M11-FR-097; M11 §6.4.15, §7.5; AWR-03 §4.3; AWR-14 §4.4, §7.9). Owner: M11.
// An overlay-phase task (Tier S 4 Hz, Tier B/A 10 Hz) walks the latest swarm columns of the page RtClient once (O(N))
// and writes the preallocated typed rows in place; the store only receives {version, n, alerts, minBatteryPct, byState,
// byOwner, inAir, stale, ...} when something changed (new swarm sample, roster version, link state or staleness), so the
// DroneRail re-renders at most at the summary rate (M15-AC-021). Rows are in swarm order; fleetIdOf / fleetModelOf /
// fleetRowOf translate through the roster. Rows are stale when the link is not LIVE or no swarm sample arrived for
// input.staleAfterMs (AWR-14 §7.9: "信号延迟" and the dashed STALE styling).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { INPUT } from '@/lib/tokens/input.gen'
import { loop } from '@/engine/loop'
import { FlightFlags, redHighlight } from '@awr/contracts/enums'
import { rtClient, type ConnState, type RosterView, type SwarmSnapshot } from '@/net/rt'

export interface FleetSummary {
  /** increments on every write (the DronesPanel cache key) */
  version: number
  n: number
  /** rows with red highlight (CORRECTING..CRASHED except LANDED, or ALERT; AWR-17 §6.5) */
  alerts: number
  /** lowest known battery percentage; NaN when every battery is unknown (255) */
  minBatteryPct: number
  /** count per FlightState 0..13 */
  byState: Uint16Array
  /** count per owner enum 0..7 */
  byOwner: Uint16Array
  /** rows with IN_AIR */
  inAir: number
  /** rows with an unknown battery (255) */
  batteryUnknown: number
  /** link not LIVE, or no swarm sample for input.staleAfterMs */
  stale: boolean
  /** roster version used for ids and models */
  rosterVersion: number
  /** sample time of the rows (simulation ms) */
  tSimMs: number
}

export const FLEET_CAPACITY = 1024
export const FLEET_STALE_MS = INPUT.staleAfterMs
export const fleetRows = {
  n: 0,
  agentNo: new Uint16Array(FLEET_CAPACITY),
  /** FlightState (low 5 bits) */
  fs: new Uint8Array(FLEET_CAPACITY),
  /** sub-mode (high 3 bits of the flight_state byte) */
  sub: new Uint8Array(FLEET_CAPACITY),
  battery: new Uint8Array(FLEET_CAPACITY),
  owner: new Uint8Array(FLEET_CAPACITY),
  /** 1 for red highlight */
  alert: new Uint8Array(FLEET_CAPACITY),
  /** 1 while the rows are stale */
  stale: new Uint8Array(FLEET_CAPACITY),
  /** FlightFlags byte (ARMED, IN_AIR, LOC_OK, FAILSAFE, GCS_LINK, FCU_LINK, LOC_DEGRADED, ALERT) */
  flags: new Uint8Array(FLEET_CAPACITY),
}

export const fleetStore = createAwrStore<FleetSummary>('fleet', () => ({
  version: 0, n: 0, alerts: 0, minBatteryPct: Number.NaN, byState: new Uint16Array(14), byOwner: new Uint16Array(8), inAir: 0,
  batteryUnknown: 0, stale: true, rosterVersion: 0, tSimMs: 0,
}))

export function useFleet<T>(selector: (s: FleetSummary) => T): T {
  return useStore(fleetStore, selector)
}

/** what the summary reads from the realtime client (tests pass a stub) */
export interface FleetSource {
  readonly swarm: SwarmSnapshot
  readonly roster: RosterView
  readonly status: ConnState
}

/** row index of an agent number: Int16 table over the u16 agent space (-1 when absent) */
const rowOfAgent = new Int16Array(65536).fill(-1)

/** vehicle id of row i (roster; the agent number as text until the roster arrives) */
export function fleetIdOf(i: number, src: FleetSource | null = rtClient()): string {
  const no = fleetRows.agentNo[i]
  return src?.roster.idOf(no) ?? String(no)
}
/** vehicle model of row i ('' until the roster arrives) */
export function fleetModelOf(i: number, src: FleetSource | null = rtClient()): string {
  return src?.roster.get(fleetRows.agentNo[i])?.model ?? ''
}
/** row of an agent number, -1 when not in the rows */
export function fleetRowOf(agentNo: number): number {
  const r = rowOfAgent[agentNo & 0xffff]
  return r < fleetRows.n ? r : -1
}
/** row of a vehicle id, -1 when unknown */
export function fleetRowOfId(id: string, src: FleetSource | null = rtClient()): number {
  const no = src?.roster.agentNoOf(id) ?? -1
  return no < 0 ? -1 : fleetRowOf(no)
}

const byState = new Uint16Array(14)
const byOwner = new Uint16Array(8)
let seenSwarm = -1
let seenRoster = -1
let seenStatus: ConnState | '' = ''
let seenStale = true
let lastSwarmAtMs = Number.NEGATIVE_INFINITY

/** reset the change detection (tests, client replaced) */
export function resetFleetSummary(): void {
  seenSwarm = -1
  seenRoster = -1
  seenStatus = ''
  seenStale = true
  lastSwarmAtMs = Number.NEGATIVE_INFINITY
  for (let i = 0; i < fleetRows.n; i++) rowOfAgent[fleetRows.agentNo[i]] = -1
  fleetRows.n = 0
}

/** one summary pass (exported for tests); returns true when the store was written */
export function summariseFleet(src: FleetSource | null = rtClient(), nowMs: number = performance.now()): boolean {
  if (!src) return false
  const sw = src.swarm
  if (sw.version !== seenSwarm) lastSwarmAtMs = nowMs
  const live = src.status === 'LIVE'
  const stale = !live || nowMs - lastSwarmAtMs > FLEET_STALE_MS
  if (sw.version === seenSwarm && src.roster.version === seenRoster && src.status === seenStatus && stale === seenStale) return false
  seenSwarm = sw.version
  seenRoster = src.roster.version
  seenStatus = src.status
  seenStale = stale
  const n = Math.min(sw.n, FLEET_CAPACITY)
  for (let i = n; i < fleetRows.n; i++) rowOfAgent[fleetRows.agentNo[i]] = -1
  byState.fill(0)
  byOwner.fill(0)
  let alerts = 0
  let inAir = 0
  let unknownBat = 0
  let minBat = Number.POSITIVE_INFINITY
  const st = stale ? 1 : 0
  for (let i = 0; i < n; i++) {
    const fsb = sw.fs[i]
    const fs = fsb & 0x1f
    const flags = sw.flags[i]
    const own = sw.ctrl[i] & 7
    const bat = sw.battery[i]
    const no = sw.agentNo[i]
    if (i < fleetRows.n && fleetRows.agentNo[i] !== no && rowOfAgent[fleetRows.agentNo[i]] === i) rowOfAgent[fleetRows.agentNo[i]] = -1
    fleetRows.agentNo[i] = no
    rowOfAgent[no] = i
    fleetRows.fs[i] = fs
    fleetRows.sub[i] = fsb >>> 5
    fleetRows.battery[i] = bat
    fleetRows.owner[i] = own
    fleetRows.flags[i] = flags
    const red = redHighlight(fs, flags) ? 1 : 0
    fleetRows.alert[i] = red
    fleetRows.stale[i] = st
    alerts += red
    if (flags & FlightFlags.IN_AIR) inAir++
    if (fs < 14) byState[fs]++
    byOwner[own]++
    if (bat === 255) unknownBat++
    else if (bat < minBat) minBat = bat
  }
  fleetRows.n = n
  const prev = fleetStore.getState()
  fleetStore.setState({
    version: prev.version + 1, n, alerts, minBatteryPct: Number.isFinite(minBat) ? minBat : Number.NaN,
    byState: byState.slice(), byOwner: byOwner.slice(), inAir, batteryUnknown: unknownBat, stale, rosterVersion: src.roster.version, tSimMs: sw.tSimMs,
  })
  return true
}

loop.register('overlay', 'fleet-summary.s', () => void summariseFleet(), { fps: 4, tiers: ['S'] })
loop.register('overlay', 'fleet-summary.ba', () => void summariseFleet(), { fps: 10, tiers: ['A', 'B'] })
