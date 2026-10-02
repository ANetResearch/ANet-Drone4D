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

/** reason code of an event (0 when none): data.code, else a reason name */
export function reasonOf(e: Pick<RtEvent, 'data'>): string {
  const d = e.data ?? {}
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
      const key = `event.type.${e.type}`
      const msg = typeof d.message === 'string' ? sanitizeText(d.message) : ''
      const base = hasKey(key) ? t(key) : sanitizeText(e.type, 64)
      return [who, base, msg].filter(Boolean).join(' · ')
    }
  }
}
