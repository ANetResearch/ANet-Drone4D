// Connection and session view (M15-FR-037, FR-033, §6.6.5; AWR-14 §4.1, §6.12, §7.7, §7.8): one UI-internal store fed by
// the page RtClient (M11): connection state with the reconnect attempt and delay (onConnState), the serverInfo view
// (role, seat, session world, run, mode, clock caps), status banners (onStatus, replaced by id, at most 3 shown) and the
// global epoch of TIME. Written on events only (never per frame); the latest TIME values (sim time, rate, state) live
// in the mutable `timeView` object for C-class text bindings, not in the store.
// The disconnection banner appears INPUT.offlineBannerDelayMs after the link left LIVE (short drops never flash it); a
// new epoch after the first one raises one toast and prunes the selection (AWR-14 §7.7 reconciliation).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { INPUT } from '@/lib/tokens/input.gen'
import type { ConnInfo, ConnState, RtClient, ServerInfoView, StatusItem, TimeFrameView } from '@/net/rt'
import { TEST_SWITCHES } from '@/lib/testSwitches'

export type Role = 'viewer' | 'operator' | 'admin'
export type Seat = 'held' | 'none' | 'other'
export interface ConnViewState {
  conn: ConnState
  attempt: number
  nextInMs: number
  code: number | null
  /** wall ms when the link left LIVE or DEGRADED (NaN while connected) */
  downSinceMs: number
  /** the offline banner is due (down for longer than INPUT.offlineBannerDelayMs) */
  bannerVisible: boolean
  role: Role | null
  seat: Seat | null
  sessionWorldId: string | null
  runId: string | null
  connId: string | null
  mode: 'live' | 'replay'
  clock: { pausable: boolean; maxSpeed: number; steppable: boolean }
  epoch: number
  /** time state (TIME.state with bit7 masked) and rate, written only when they change */
  timeState: number
  rate: number
  status: readonly StatusItem[]
  version: number
}

export const connViewStore = createAwrStore<ConnViewState>('ui.conn', () => ({
  conn: 'IDLE', attempt: 0, nextInMs: 0, code: null, downSinceMs: Number.NaN, bannerVisible: false,
  role: null, seat: null, sessionWorldId: null, runId: null, connId: null, mode: 'live',
  clock: { pausable: true, maxSpeed: 10, steppable: true }, epoch: -1, timeState: 0, rate: 1, status: [], version: 0,
}))

export function useConnView<T>(sel: (s: ConnViewState) => T): T {
  return useStore(connViewStore, sel)
}

/** latest TIME (10 Hz) for bound text; mutated in place, never stored */
export const timeView = { tSimMs: Number.NaN, state: 0, rate: 1, epoch: -1, atMs: Number.NaN }

const ONLINE: ReadonlySet<ConnState> = new Set(['LIVE', 'DEGRADED'])
export const isOnline = (c: ConnState): boolean => ONLINE.has(c)

/** the session may send write calls: connected, a writing role holding the seat, live mode (AWR-14 §7.8) */
export function canWriteOf(s: Pick<ConnViewState, 'conn' | 'role' | 'seat' | 'mode'>): boolean {
  return isOnline(s.conn) && s.role !== null && s.role !== 'viewer' && s.seat === 'held' && s.mode === 'live'
}
export const useCanWrite = (): boolean => useConnView(canWriteOf)
export const canWriteNow = (): boolean => canWriteOf(connViewStore.getState())

/** display role: an operator whose seat is held elsewhere reads as read-only (AWR-14 §4.1) */
export function roleKeyOf(s: Pick<ConnViewState, 'role' | 'seat'>): 'role.viewer' | 'role.operator' | 'role.admin' | 'role.viewerSeatTaken' | 'role.unknown' {
  if (s.role === null) return 'role.unknown'
  if (s.role === 'viewer') return 'role.viewer'
  if (s.seat !== 'held') return 'role.viewerSeatTaken'
  return s.role === 'admin' ? 'role.admin' : 'role.operator'
}

function applyServerInfo(si: ServerInfoView | null): Partial<ConnViewState> {
  if (!si) return {}
  return {
    role: si.role, seat: si.seat, sessionWorldId: si.worldId || null, runId: si.runId || null, connId: si.connId || null,
    mode: si.mode === 'replay' ? 'replay' : 'live',
    clock: { pausable: si.clock?.pausable !== false, maxSpeed: si.clock?.maxSpeed ?? 10, steppable: si.clock?.steppable !== false },
  }
}

export interface ConnViewHooks {
  /** a new epoch arrived after the first one (sim-core restart, scenario reset) */
  onEpochChange?: (from: number, to: number) => void
}

let bannerTimer: ReturnType<typeof setTimeout> | null = null
function patch(p: Partial<ConnViewState>): void {
  const s = connViewStore.getState()
  let changed = false
  for (const k of Object.keys(p) as (keyof ConnViewState)[]) {
    if (s[k] !== p[k]) {
      changed = true
      break
    }
  }
  if (changed) connViewStore.setState({ ...p, version: s.version + 1 })
}

/** state transition of the connection (exported for tests) */
export function onConnState(st: ConnState, info: ConnInfo | null, nowMs = Date.now()): void {
  const s = connViewStore.getState()
  const online = isOnline(st)
  const p: Partial<ConnViewState> = { conn: st, attempt: info?.attempt ?? 0, nextInMs: info?.nextInMs ?? 0, code: info?.code ?? null }
  if (online) {
    p.downSinceMs = Number.NaN
    p.bannerVisible = false
    if (bannerTimer) {
      clearTimeout(bannerTimer)
      bannerTimer = null
    }
  } else if (st === 'RECONNECTING' || st === 'CLOSED' || st === 'FATAL') {
    // a first connection that never went live counts from now as well
    if (!Number.isFinite(s.downSinceMs)) p.downSinceMs = nowMs
    if (st === 'FATAL') p.bannerVisible = true
    else if (!s.bannerVisible && !bannerTimer) {
      bannerTimer = setTimeout(() => {
        bannerTimer = null
        const cur = connViewStore.getState()
        if (!isOnline(cur.conn) && cur.conn !== 'IDLE') patch({ bannerVisible: true })
      }, INPUT.offlineBannerDelayMs)
    }
  }
  patch(p)
}

/** TIME at 10 Hz: mutable view, the store only on state, rate or epoch changes */
export function onTime(t: TimeFrameView, hooks: ConnViewHooks = {}): void {
  timeView.tSimMs = t.tSimMs
  timeView.state = t.state & 0x0f
  timeView.rate = t.rate
  timeView.atMs = performance.now()
  const s = connViewStore.getState()
  if (t.epoch !== timeView.epoch) {
    const prev = timeView.epoch
    timeView.epoch = t.epoch
    if (prev >= 0 && t.epoch !== prev) hooks.onEpochChange?.(prev, t.epoch)
  }
  if (s.timeState !== timeView.state || s.rate !== t.rate || s.epoch !== t.epoch) patch({ timeState: timeView.state, rate: t.rate, epoch: t.epoch })
}

/** the client delivers the whole current list (status replaces by id, removeStatus drops); at most 3 are shown */
export function onStatusItems(items: readonly StatusItem[]): void {
  const next = items.filter((x) => typeof x.message === 'string' && x.message !== '').slice(-3)
  const cur = connViewStore.getState().status
  if (next.length === cur.length && next.every((x, i) => x.id === cur[i].id && x.message === cur[i].message && x.level === cur[i].level)) return
  patch({ status: next })
}

/** attach to the page RtClient; returns the detach function (RtProvider effect) */
export function installConnView(rt: RtClient, hooks: ConnViewHooks = {}): () => void {
  patch({ conn: rt.status, ...applyServerInfo(rt.serverInfo) })
  // test builds: specs can put the view into states the fake source cannot produce (viewer, seat taken, offline banner)
  if (TEST_SWITCHES && typeof window !== 'undefined') {
    const w = window as unknown as { __uxInject?: Record<string, unknown> }
    w.__uxInject = { ...w.__uxInject, conn: (p: Partial<ConnViewState>) => patch(p) }
  }
  const offs = [
    rt.onConnState((st, info) => onConnState(st, info)),
    rt.onServerInfo((si) => patch(applyServerInfo(si))),
    rt.onStatus((items) => onStatusItems(items)),
    rt.onTime((t) => onTime(t, hooks)),
  ]
  return () => {
    for (const off of offs) off()
    if (bannerTimer) {
      clearTimeout(bannerTimer)
      bannerTimer = null
    }
  }
}

/** age of the last TIME in seconds (STALE presentation after INPUT.staleAfterMs), 0 while fresh */
export function timeAgeS(nowMs = performance.now()): number {
  if (!Number.isFinite(timeView.atMs)) return 0
  const age = nowMs - timeView.atMs
  return age > INPUT.staleAfterMs ? age / 1000 : 0
}
