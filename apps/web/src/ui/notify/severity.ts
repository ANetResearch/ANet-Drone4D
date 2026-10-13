// Event severity and display text (M15-FR-036, FR-088; AWR-14 §11.1, §11.6, §13.2; AWR-17 §6.12). Pure functions shared by
// the event log, the alarm merger, the toast merger and the Timeline markers.
// Severity: the wire level (0 INFO, 1 NOTICE -> info; 2 WARNING -> warning; 3 CRITICAL -> critical), raised by the
// FlightState severity of `uav.state` targets (g04 §3.1: CORRECTING 1 .. LANDING(FAILSAFE) 4 are warning, ELAND 5,
// FAILSAFE 6, CRASHED 8 are critical). The rank orders criticals for the one-red arbitration (RedArbiter).
import { FlightState, SEVERITY } from '@awr/contracts/enums'
import { hasKey, reasonText, t } from '@/app/i18n'
import { sanitizeText } from '@/lib/sanitize'
import type { RtEvent } from '@/net/rt'

export type Sev = 'info' | 'warning' | 'critical'
export const SEV_RANK: Readonly<Record<Sev, number>> = { info: 0, warning: 1, critical: 2 }

/** FlightState value of a `uav.state` target such as "HOLD/LINK_LOSS" (-1 when unknown) */
export function flightStateOf(to: unknown): number {
  if (typeof to !== 'string') return -1
  const name = to.split('/')[0]
  const v = (FlightState as Readonly<Record<string, number>>)[name]
  return typeof v === 'number' ? v : -1
}

/** sub mode name of a "STATE/SUB" pair ('' when absent) */
export function subOf(to: unknown): string {
  if (typeof to !== 'string') return ''
  const i = to.indexOf('/')
  const s = i >= 0 ? to.slice(i + 1) : ''
  return s === 'None' || s === 'null' ? '' : s
}

export interface SevInfo { sev: Sev; rank: number }
const scratch: SevInfo = { sev: 'info', rank: 0 }

/** severity and rank of one event; writes into `out` (no allocation on the bridge path) */
export function eventSeverity(e: Pick<RtEvent, 'type' | 'level' | 'data'>, out: SevInfo = scratch): SevInfo {
  let sev: Sev = e.level >= 3 ? 'critical' : e.level === 2 ? 'warning' : 'info'
  let rank: number = e.level
  if (e.type === 'uav.state') {
    const fs = flightStateOf(e.data?.to)
    const r = fs >= 0 ? (SEVERITY[fs] ?? 0) : 0
    // LANDING with the FAILSAFE flag is severity 4; a plain operator landing arrives with a low wire level
    if (r >= 5) sev = 'critical'
    else if (r >= 1 && !(fs === FlightState.LANDING && e.level < 2)) sev = SEV_RANK[sev] < 1 ? 'warning' : sev
    rank = Math.max(rank, r)
  } else if (e.type === 'cmd.rejected' || e.type === 'cmd.failed' || e.type === 'cmd.timeout') {
    if (sev === 'info') sev = 'warning'
  } else if (e.type === 'target.discovered') {
    // D2 (AWR-04 §10.7): a discovery is critical with rank 5 (the ELAND rank; equal ranks: the latest wins)
    sev = 'critical'
    rank = Math.max(rank, 5)
  } else if (e.type === 'proc.state' && e.data?.state === 'FAILED') {
    sev = 'critical'
    rank = Math.max(rank, 9)
  }
  out.sev = sev
  out.rank = rank
  return out
}

/** event source (first segment of the type, AWR-14 §11.4 AlarmItem.source) */
export const sourceOf = (type: string): string => {
  const i = type.indexOf('.')
  return i > 0 ? type.slice(0, i) : type
}

/** reason code of an event (0 when none): data.code, else a reason name; a target discovery merges per target (D2,
 * AWR-14 §20.11.1 merge key target:discovered:<target>) */
export function reasonOf(e: Pick<RtEvent, 'data'> & Partial<Pick<RtEvent, 'type'>>): string {
  const d = e.data ?? {}
  if (e.type === 'target.discovered' && typeof d.target === 'string') return d.target
  if (typeof d.code === 'number' && d.code > 0) return String(d.code)
  if (typeof d.reason === 'string' && d.reason) return d.reason
  if (typeof d.to === 'string') return subOf(d.to) || String(d.to).split('/')[0]
  return '0'
}

/** merge key source:type:reason (AWR-14 §11.4) */
export const mergeKeyOf = (e: Pick<RtEvent, 'type' | 'data'>): string => `${sourceOf(e.type)}:${e.type}:${reasonOf(e)}`

/** FlightState display name (AWR-14 §13.2) */
/** sub-state name of a flight state (contracts FlightSub, e.g. RTL/CRUISE -> 巡航); unknown names are sanitised verbatim */
export function subStateText(sub: string): string {
  const key = `fs.sub.${sub}`
  return hasKey(key) ? t(key) : sanitizeText(sub, 32)
}

export function flightStateText(fs: number): string {
  return fs >= 0 && fs <= 13 ? t(`fs.${fs}`) : t('fs.0')
}

const OPS = new Set(['takeoff', 'land', 'goto', 'hover', 'rtl', 'follow_path', 'orbit', 'safety_stop', 'resume', 'pause', 'acquire', 'release', 'cancel', 'arm', 'disarm'])
export const opText = (op: unknown): string => (typeof op === 'string' && OPS.has(op) ? t(`cmd.op.${op}`) : typeof op === 'string' ? sanitizeText(op, 32) : '')

/** one line of human text for the event table and toasts (sanitised; texts from the wire never pass unfiltered) */
export function describeEvent(e: Pick<RtEvent, 'type' | 'data' | 'uav'>): string {
  const d = e.data ?? {}
  const who = e.uav ? sanitizeText(e.uav, 64) : ''
  switch (e.type) {
    case 'uav.state': {
      const fs = flightStateOf(d.to)
      const sub = subOf(d.to)
      return t('event.uavState', { who, state: flightStateText(fs), sub: sub ? ` · ${subStateText(sub)}` : '' })
    }
    case 'sim.vehicle.state':
      return t('event.vehicleState', { who, to: sanitizeText(typeof d.to === 'string' ? d.to : '', 32) })
    case 'cmd.accepted': case 'cmd.running': case 'cmd.succeeded': case 'cmd.failed': case 'cmd.canceled': case 'cmd.timeout': case 'cmd.rejected': {
      const status = e.type.slice(4)
      const code = typeof d.code === 'number' ? d.code : 0
      const reason = code > 0 ? ` · ${code} ${reasonText(code).short}` : ''
      return t('event.cmd', { who, op: opText(d.op), status: t(`command.state.${status}`), reason })
    }
    default: {
      const d2 = describeD2(e)
      if (d2 !== null) return d2
      const key = `event.type.${e.type}`
      const msg = typeof d.message === 'string' ? sanitizeText(d.message) : ''
      const base = hasKey(key) ? t(key) : sanitizeText(e.type, 64)
      return [who, base, msg].filter(Boolean).join(' · ')
    }
  }
}

const d2s = (v: unknown): string => (typeof v === 'string' ? sanitizeText(v, 64) : '')
const d2n = (v: unknown, digits = 0): string => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—')
const d2kind = (v: unknown): string => (v === 'visual' || v === 'acoustic' || v === 'radar' || v === 'rf' ? t(`sens.kind.${v}`) : d2s(v))
const d2level = (v: unknown): string => (v === 'D' || v === 'R' || v === 'I' ? t(`perception.level.${v}`) : d2s(v))

/** event table text of the D2 events (AWR-17 §17.7.5; AWR-14 §20.11.1), null for other types */
export function describeD2(e: Pick<RtEvent, 'type' | 'data' | 'uav'>): string | null {
  const d = e.data ?? {}
  const uav = d2s(d.uav ?? e.uav)
  switch (e.type) {
    case 'target.discovered':
      return t('target.discovered', { target: d2s(d.target), uav, kind: d2kind(d.kind), range: d2n(d.range_m) })
    case 'target.state':
      return t('event.d2.targetState', { target: d2s(d.target), to: t(`awareness.${typeof d.to === 'string' ? d.to : 'UNAWARE'}`) })
    case 'perception.capture':
      return t('perception.captured', { target: d2s(d.target), uav, sensor: d2s(d.sensor), level: d2level(d.level_to) })
    case 'perception.capture_lost':
      return t('event.d2.captureLost', { target: d2s(d.target), uav, sensor: d2s(d.sensor) })
    case 'perception.level':
      return t('event.d2.level', { target: d2s(d.target), uav, level: d2level(d.level_to), n: d2n(d.n_eff, 1), req: d2n(d.n_req, 0) })
    case 'perception.detect':
      return t('event.d2.detect', { uav, sensor: d2s(d.sensor), level: d2level(d.level) })
    case 'uav.keepout_guard':
      return t('event.d2.keepout', { uav, target: d2s(d.target), d: d2n(d.d_m, 1) })
    case 'nest.dispatch_delayed':
      return t('event.d2.nestDelayed', { nest: d2s(d.nest), reason: d2s(d.reason), s: d2n(d.delay_s) })
    case 'nest.launch':
    case 'nest.recover':
    case 'nest.swap_done':
      return t(`event.d2.${e.type === 'nest.launch' ? 'nestLaunch' : e.type === 'nest.recover' ? 'nestRecover' : 'nestSwap'}`, { nest: d2s(d.nest), uav })
    case 'relay.dispatch':
      return t('event.d2.relayDispatch', { uav, target: d2s(d.target), reason: d2s(d.reason) })
    case 'relay.handover':
      return t('event.d2.relayHandover', { from: d2s(d.from_uav), to: d2s(d.to_uav), s: d2n(d.overlap_s) })
    case 'relay.gap':
      return t('event.d2.relayGap', { s: d2n(d.duration_s), cause: d2s(d.cause) })
    case 'task.created':
    case 'task.state':
      return t('event.d2.task', { tid: d2s(d.tid), to: d2s(d.to) })
    case 'task.allocated':
    case 'task.reallocated':
      return t('event.d2.taskAllocated', { tid: d2s(d.tid) })
    case 'fw.wind_exceeds':
      return t('event.d2.fwWind', { uav, wind: d2n(d.wind_mps, 1) })
    case 'sensor.mode': {
      // d2-WP-10-to-M15 item 2: {uav, sensor, from, to, reason}
      const mode = (v: unknown): string => (typeof v === 'string' && hasKey(`sensor.mode.${v}`) ? t(`sensor.mode.${v}`) : d2s(v))
      const why = d2s(d.reason)
      const args = { uav, sensor: d2s(d.sensor), from: mode(d.from), to: mode(d.to), why }
      return why ? t('event.d2.sensorModeWhy', args) : t('event.d2.sensorMode', args)
    }
    case 'sandbox.expiring':
      return t('event.d2.expiring', { s: d2n(d.in_s), reason: t(`sandbox.endReason.${typeof d.reason === 'string' ? d.reason : 'lifetime'}`) })
    case 'sandbox.rate':
      return t('event.d2.rate', { granted: d2n(d.granted, 2), requested: d2n(d.requested, 2) })
    case 'sandbox.state':
      return t('event.d2.sandboxState', { to: d2s(d.to) })
    default:
      return null
  }
}

/** the four D2 filters of the events tab (AWR-14 §20.11.1): perception, targets, tasking, sandbox */
export type D2Filter = 'perception' | 'targets' | 'tasking' | 'sandbox'
export function d2FilterOf(type: string): D2Filter | null {
  if (type.startsWith('perception.')) return 'perception'
  if (type.startsWith('target.') || type === 'uav.keepout_guard') return 'targets'
  if (type.startsWith('task.') || type.startsWith('relay.') || type.startsWith('nest.') || type === 'fw.wind_exceeds') return 'tasking'
  if (type.startsWith('sandbox.') || type === 'session.switched') return 'sandbox'
  return null
}
