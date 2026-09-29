// Client-side admission pre-check (M15-FR-027, FR-040; AWR-14 §6.7, §6.11 "READY -> READY: pre-check denies, Tooltip
// reason"): the admission matrix of packages/contracts/rt/commands.json (generated admitSymbol) against the vehicle's
// latest FlightState and flags from the swarm columns of the page RtClient. It only greys buttons and explains why; the
// server stays the only admission authority (U-03). Conditions (for example sub_ready_to_arm) are left to the server.
import { admitSymbol, SERVICE_BY_OP } from '@awr/contracts/commands'
import { FlightState } from '@awr/contracts/enums'
import { rtClient } from '@/net/rt'
import { fleetRowOfId, fleetRows } from '@/stores/fleet'

export interface Precheck { ok: boolean; reasonKey: string | null; params?: Record<string, string | number> }
const OK: Precheck = { ok: true, reasonKey: null }
const GROUNDED = new Set<number>([FlightState.UNKNOWN, FlightState.DISARMED, FlightState.PREFLIGHT, FlightState.READY, FlightState.LANDED])
const NAV = new Set(['goto', 'orbit', 'follow_path', 'velocity'])

/** pure matrix decision (exported for tests) */
export function precheckState(op: string, fs: number, flags: number): Precheck {
  const s = SERVICE_BY_OP[op]
  if (!s?.admission || fs < 0 || fs > 13) return OK
  const [sym] = admitSymbol(op, fs, flags)
  if (sym === 'Y') return OK
  if (sym === 'S') return { ok: false, reasonKey: 'admit.safety' }
  if (sym === '=') return { ok: false, reasonKey: 'admit.running' }
  if (NAV.has(op) && GROUNDED.has(fs)) return { ok: false, reasonKey: 'admit.airborneFirst' }
  if (op === 'takeoff' && !GROUNDED.has(fs)) return { ok: false, reasonKey: 'admit.airborne' }
  return { ok: false, reasonKey: 'admit.state', params: { state: fs } }
}

/** index of an agent in the latest swarm columns (-1 when unknown); n <= 1024, a linear scan */
export function swarmIndexOf(agentNo: number): number {
  const rt = rtClient()
  if (!rt || agentNo < 0) return -1
  const sw = rt.swarm
  for (let i = 0; i < sw.n; i++) if (sw.agentNo[i] === agentNo) return i
  return -1
}

/** FlightState and flags of a vehicle id (fleet rows of M11, else the live swarm columns); null when unknown */
export function vehicleState(id: string): { fs: number; sub: number; flags: number; owner: number; battery: number } | null {
  const r = fleetRowOfId(id)
  if (r >= 0) return { fs: fleetRows.fs[r], sub: fleetRows.sub[r], flags: fleetRows.flags[r], owner: fleetRows.owner[r], battery: fleetRows.battery[r] }
  const rt = rtClient()
  if (!rt) return null
  const i = swarmIndexOf(rt.roster.agentNoOf(id))
  if (i < 0) return null
  const sw = rt.swarm
  return { fs: sw.fs[i] & 0x1f, sub: sw.fs[i] >>> 5, flags: sw.flags[i], owner: sw.ctrl[i] & 7, battery: sw.battery[i] }
}

/** pre-check of a single-vehicle op; unknown vehicles pass (the server answers) */
export function precheck(op: string, id: string | null): Precheck {
  if (!id) return { ok: false, reasonKey: 'admit.noFocus' }
  const v = vehicleState(id)
  return v ? precheckState(op, v.fs, v.flags) : OK
}
