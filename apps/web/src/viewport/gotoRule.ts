// GoTo target and single-vehicle command path (AWR-14 §6.7 default altitude rule, §6.11; AWR-17 §7.1, §7.2). Owner: M06.
// Target = directly above the picked point; keep the vehicle's current world z but never below hit z + 10 m. The call
// `uav/{id}/cmd/goto {pos, speed_mps, route}` drives the GoTo marker (accepted, running, succeeded, failed/rejected).
// sendVehicleCommand sends the other single-vehicle commands of the detail page (takeoff, hover, land, rtl) for the
// primary selection and keeps the last result per op for the UI and test hooks. Every command marks cmd.sent for the
// cmdToVisibleMs latency metric (M06 §6.16).
import { latency } from '@/engine'
import { rtClient, type CallHandle, type CallResult, type CallStatus } from '@/net/rt'
import { selectionStore } from '@/stores/selection'
import { vp } from './session'

export const GOTO = { minAboveHitM: 10, minAltAboveHitM: 2, route: 'auto' as const } as const

/** agent number of the primary selection (roster id, or a numeric id before the roster), -1 when none */
export function primaryAgentNo(): number {
  const id = selectionStore.getState().primary
  if (id === null) return -1
  const rt = rtClient()
  const no = rt?.roster.agentNoOf(id) ?? -1
  if (no >= 0) return no
  return /^\d+$/.test(id) ? Number(id) : -1
}

const pose = new Float64Array(3)
/** target above the surface point for the given vehicle (ENU m) */
export function gotoTargetFor(surface: ArrayLike<number>, agentNo: number): [number, number, number] {
  const floor = surface[2] + GOTO.minAboveHitM
  let z = floor
  if (agentNo >= 0 && vp.drones?.poseOf(agentNo, pose)) z = Math.max(pose[2], floor)
  return [surface[0], surface[1], z]
}

export interface GotoRequest { id: string; target: [number, number, number]; handle: CallHandle }

/** result statuses of the last goto in arrival order (accepted, running, succeeded; test hooks and diagnostics) */
export const gotoResults: string[] = []

function markSent(agentNo: number, kind: 'takeoff' | 'goto' | 'land' | 'hover', target?: ArrayLike<number>): void {
  const z = vp.drones?.poseOf(agentNo, pose) ? pose[2] : 0
  latency().markCmd(agentNo, kind, performance.now(), z, target)
}

/** send goto for the primary selection to the current ground pick; null when a precondition is missing */
export type GotoRoute = 'auto' | 'direct' | 'safe_transit'
export interface GotoOptions {
  speedMps?: number
  /** AWR-14 §6.7 route (default auto) */
  route?: GotoRoute
  /** "h m above the hit" altitude mode: target z = hit z + h (at least 2 m); default: the vehicle's z, >= hit z + 10 m */
  altAboveHitM?: number
}

export function sendGoto(o: GotoOptions = {}): GotoRequest | null {
  const rt = rtClient()
  const pick = vp.pick
  const no = primaryAgentNo()
  if (!rt || !pick || no < 0) return null
  const id = rt.roster.idOf(no)
  if (!id) return null
  const target = o.altAboveHitM !== undefined && Number.isFinite(o.altAboveHitM)
    ? [pick.surface[0], pick.surface[1], pick.surface[2] + Math.max(GOTO.minAltAboveHitM, o.altAboveHitM)] as [number, number, number]
    : gotoTargetFor(pick.surface, no)
  const args: Record<string, unknown> = { pos: target, route: o.route ?? GOTO.route }
  if (o.speedMps !== undefined) args.speed_mps = o.speedMps
  const handle = rt.call(`uav/${id}/cmd/goto`, args)
  markSent(no, 'goto', target)
  const marker = vp.mission?.goto ?? null
  marker?.set(pick.surface, target, 'preview', performance.now())
  gotoResults.length = 0
  handle.onResult((r: CallResult) => {
    gotoResults.push(r.status)
    const now = performance.now()
    const m = vp.mission?.goto ?? marker
    if (!m) return
    if (r.status === 'accepted') m.setState('accepted', now)
    else if (r.status === 'running') m.setState('running', now)
    else if (r.status === 'succeeded') m.setState('succeeded', now)
    else m.setState('failed', now)
    vp.changed()
  })
  vp.pick = null
  vp.changed()
  return { id, target, handle }
}

/** single-vehicle commands of the detail page command group besides GoTo (AWR-14 §4.4 command area) */
export type VehicleCmd = 'takeoff' | 'hover' | 'land' | 'rtl'
/** status 'sent' until the first result (accepted or rejected) arrives; `results` lists every status in order */
export interface VehicleCmdState { op: VehicleCmd; vehicle: string; status: CallStatus | 'sent'; code: number; reason: string | null; results: CallStatus[] }

const lastCmd = new Map<VehicleCmd, VehicleCmdState>()

/** last result of op for the primary selection (null when none was sent for the current primary) */
export function vehicleCmdState(op: VehicleCmd): VehicleCmdState | null {
  const s = lastCmd.get(op)
  return s && s.vehicle === selectionStore.getState().primary ? s : null
}

/** send `uav/{id}/cmd/{op}` for the primary selection; null when there is no primary or no realtime client */
export function sendVehicleCommand(op: VehicleCmd, args: Record<string, unknown> = {}): CallHandle | null {
  const rt = rtClient()
  const no = primaryAgentNo()
  if (!rt || no < 0) return null
  const id = rt.roster.idOf(no)
  if (!id) return null
  const handle = rt.call(`uav/${id}/cmd/${op}`, args)
  if (op !== 'rtl') markSent(no, op)
  const results: CallStatus[] = []
  lastCmd.set(op, { op, vehicle: id, status: 'sent', code: 0, reason: null, results })
  vp.changed()
  handle.onResult((r: CallResult) => {
    results.push(r.status)
    lastCmd.set(op, { op, vehicle: id, status: r.status, code: r.code, reason: r.reason ?? null, results })
    vp.changed()
  })
  return handle
}
