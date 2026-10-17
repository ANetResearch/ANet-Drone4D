// Power-save state machine, pure part (R-D2-33, R-D2-34; ADR-121; AWR-04 §15.7.1 to §15.7.3; M15-FR-227; UX-FR-152).
// Owner: M15. No timers, DOM or stores: app/power/PowerController.ts feeds it the interaction timestamps, the visibility,
// a 1 Hz tick and the link events, and performs the side effects of the transitions it returns.
//   ACTIVE  --no input quiet_s, visible-->           QUIET    (frame cap 30 fps, phone 20)
//   ACTIVE, QUIET --no input idle - warn_s-->        WARNING  (countdown bar "n s to power save, any input cancels")
//   WARNING --countdown 0-->                         SAVING   (reason idle)
//   ACTIVE, QUIET, WARNING --hidden hidden_s, pagehide, freeze--> SAVING (reason hidden)
//   any --server closed 4430-->                      SAVING   (reason server; no request needed)
//   SAVING --any input-->                            RESUMING (the first input itself still takes effect)
//   RESUMING --link live / sandbox resumed-->        ACTIVE
//   RESUMING --viewer seats full / sandbox queued--> QUEUED
//   QUEUED --seat_ready / slot_ready-->              RESUMING
// External API activity of the sandbox session (issue #59; `sandbox/session.api_activity_age_s`: SDK, scripts and other
// clients, never the page's own requests) counts like an interaction for the idle and hidden rules: it restarts the idle
// clock, cancels the warning and keeps a hidden page from saving while it lasts; QUIET (the frame cap) and presence follow the
// local input only.
// Static browsing, the landing page, pages without a realtime link and the local build (role `none`) never save (nothing
// to reclaim) and keep their frame rate (no quiet cap: benchmarks and soak runs of the local build run unattended). The
// internal identity's "keep running" never warns or saves (the quiet cap still applies).

export type PowerState = 'ACTIVE' | 'QUIET' | 'WARNING' | 'SAVING' | 'RESUMING' | 'QUEUED'
export type PowerRole = 'viewer' | 'sandbox' | 'none'
export type SaveReason = 'idle' | 'hidden' | 'server'

export interface PowerThresholds {
  viewerIdleS: number
  viewerIdleBusyS: number
  sandboxIdleS: number
  sandboxIdleBusyS: number
  hiddenS: number
  warnS: number
  quietS: number
  graceS: number
  presenceMinS: number
  internalIdleS: number
}

/** AWR-04 §15.7.2 defaults (the server's capacity `power` and serverInfo.power override them) */
export const POWER_DEFAULTS: Readonly<PowerThresholds> = {
  viewerIdleS: 300, viewerIdleBusyS: 120, sandboxIdleS: 600, sandboxIdleBusyS: 300, hiddenS: 60, warnS: 30, quietS: 60, graceS: 90, presenceMinS: 15,
  internalIdleS: 3600,
}

/** pointer movement below this many CSS px (accumulated) is jitter, not an interaction (AWR-04 §15.7.1) */
export const JITTER_PX = 8
/** a window resize of at least this fraction counts as an interaction */
export const RESIZE_FRAC = 0.1
/** a lone modifier key counts only when another key follows within this time */
export const MODIFIER_WINDOW_MS = 2000

export interface PowerContext {
  role: PowerRole
  /** the sandbox queue or the viewer queue is not empty (the busy thresholds apply) */
  busy: boolean
  internal: boolean
  keepRunning: boolean
}

export interface Transition { from: PowerState; to: PowerState; reason: SaveReason | 'input' | 'activity' | 'quiet' | 'warn' | 'live' | 'queued' | 'ready' }

/** idle threshold (s) of a context */
export function idleThresholdS(th: PowerThresholds, c: PowerContext): number {
  if (c.internal) return th.internalIdleS
  if (c.role === 'sandbox') return c.busy ? th.sandboxIdleBusyS : th.sandboxIdleS
  return c.busy ? th.viewerIdleBusyS : th.viewerIdleS
}

export class PowerMachine {
  state: PowerState = 'ACTIVE'
  reason: SaveReason | null = null
  lastInputMs: number
  /** wall ms of the latest external API activity of the session (-Infinity: none) */
  activityMs = Number.NEGATIVE_INFINITY
  /** wall ms when the page became hidden (NaN while visible) */
  hiddenSinceMs = Number.NaN
  th: PowerThresholds = { ...POWER_DEFAULTS }
  ctx: PowerContext = { role: 'none', busy: false, internal: false, keepRunning: false }
  savedAtMs = Number.NaN

  constructor(nowMs: number) {
    this.lastInputMs = nowMs
  }

  /** the machine may save (a realtime link exists and keep-running is off) */
  canSave(): boolean {
    return this.ctx.role !== 'none' && !(this.ctx.internal && this.ctx.keepRunning)
  }

  /** the idle clock's reference: the latest local input or external API activity */
  idleSinceMs(): number {
    return Math.max(this.lastInputMs, this.activityMs)
  }

  /** seconds left of the warning countdown (0 outside WARNING) */
  warnLeftS(nowMs: number): number {
    if (this.state !== 'WARNING') return 0
    const left = idleThresholdS(this.th, this.ctx) - (nowMs - this.idleSinceMs()) / 1000
    return Math.max(0, Math.ceil(left))
  }

  /** seconds since the last interaction (presence `input_age_s`; keep-running reports 0) */
  inputAgeS(nowMs: number): number {
    if (this.ctx.internal && this.ctx.keepRunning) return 0
    return Math.max(0, (nowMs - this.lastInputMs) / 1000)
  }

  private go(to: PowerState, reason: Transition['reason']): Transition | null {
    if (to === this.state) return null
    const t: Transition = { from: this.state, to, reason }
    this.state = to
    if (to === 'SAVING') this.reason = reason as SaveReason
    if (to === 'ACTIVE') this.reason = null
    return t
  }

  /** an interaction (AWR-04 §15.7.1 rules already applied by the caller) */
  input(nowMs: number): Transition | null {
    this.lastInputMs = nowMs
    if (this.state === 'QUIET' || this.state === 'WARNING') return this.go('ACTIVE', 'input')
    if (this.state === 'SAVING') return this.go('RESUMING', 'input')
    return null
  }

  /** external API activity at `atMs` (wall ms; clamped to now): restarts the idle clock and cancels the warning (back to QUIET
   *  when the local input is older than quiet_s, else ACTIVE); nothing to do while saving, resuming or queued */
  activity(atMs: number, nowMs: number): Transition | null {
    const at = Math.min(atMs, nowMs)
    if (!(at > this.activityMs)) return null
    this.activityMs = at
    if (this.state !== 'WARNING') return null
    if ((nowMs - this.idleSinceMs()) / 1000 >= idleThresholdS(this.th, this.ctx) - this.th.warnS) return null
    return this.go((nowMs - this.lastInputMs) / 1000 >= this.th.quietS ? 'QUIET' : 'ACTIVE', 'activity')
  }

  /** visibility changes (hidden starts the hidden timer; visible clears it) */
  visibility(visible: boolean, nowMs: number): void {
    if (visible) this.hiddenSinceMs = Number.NaN
    else if (!Number.isFinite(this.hiddenSinceMs)) this.hiddenSinceMs = nowMs
  }

  /** pagehide or Page Lifecycle freeze: save at once */
  pageHide(): Transition | null {
    if (!this.canSave() || this.state === 'SAVING' || this.state === 'QUEUED') return null
    return this.go('SAVING', 'hidden')
  }

  /** the server reclaimed the link (close 4430, error 518): saving without a request */
  serverReclaimed(): Transition | null {
    if (this.ctx.role === 'none') return null
    this.savedAtMs = Number.NaN
    return this.go('SAVING', 'server')
  }

  /** the link is live again (viewer handshake + SNAPSHOT) or the sandbox resumed */
  live(): Transition | null {
    if (this.state !== 'RESUMING' && this.state !== 'QUEUED') return null
    return this.go('ACTIVE', 'live')
  }

  /** the viewer seats are full (4431) or the sandbox resume got a ticket (202) */
  queued(): Transition | null {
    return this.go('QUEUED', 'queued')
  }

  /** seat_ready or slot_ready: connect again */
  ready(): Transition | null {
    if (this.state !== 'QUEUED') return null
    return this.go('RESUMING', 'ready')
  }

  /** the 1 Hz check; returns the transition it made, if any */
  tick(nowMs: number): Transition | null {
    const s = this.state
    if (s === 'SAVING' || s === 'RESUMING' || s === 'QUEUED') return null
    // hidden: saves once both the page has been hidden and the session has seen no external API activity for hidden_s
    const hiddenMs = this.th.hiddenS * 1000
    if (this.canSave() && Number.isFinite(this.hiddenSinceMs) && nowMs - this.hiddenSinceMs >= hiddenMs && nowMs - this.activityMs >= hiddenMs) {
      return this.go('SAVING', 'hidden')
    }
    if (Number.isFinite(this.hiddenSinceMs)) return null // hidden: no quiet or warning while nobody looks
    const idleS = (nowMs - this.idleSinceMs()) / 1000
    if (this.canSave()) {
      const limit = idleThresholdS(this.th, this.ctx)
      if (idleS >= limit) return this.go('SAVING', 'idle')
      if (s !== 'WARNING' && idleS >= limit - this.th.warnS) return this.go('WARNING', 'warn')
    }
    // the quiet cap belongs to the power-save roles too: the local build's benchmarks and soak runs keep their frame rate
    if (s === 'ACTIVE' && this.ctx.role !== 'none' && (nowMs - this.lastInputMs) / 1000 >= this.th.quietS) return this.go('QUIET', 'quiet')
    return null
  }
}

/** thresholds from the capacity response or serverInfo (seconds; missing fields keep the current value) */
export function thresholdsFrom(cur: PowerThresholds, p: Partial<Record<string, unknown>> | null | undefined): PowerThresholds {
  if (!p) return cur
  const num = (k: string, v: number): number => {
    const x = p[k]
    return typeof x === 'number' && Number.isFinite(x) && x > 0 ? x : v
  }
  return {
    viewerIdleS: num('viewer_idle_s', cur.viewerIdleS), viewerIdleBusyS: num('viewer_idle_busy_s', cur.viewerIdleBusyS),
    sandboxIdleS: num('sandbox_idle_s', cur.sandboxIdleS), sandboxIdleBusyS: num('sandbox_idle_busy_s', cur.sandboxIdleBusyS),
    hiddenS: num('hidden_s', cur.hiddenS), warnS: num('warn_s', cur.warnS), quietS: num('quiet_s', cur.quietS), graceS: num('grace_s', cur.graceS),
    presenceMinS: num('presence_min_s', cur.presenceMinS), internalIdleS: num('internal_idle_s', cur.internalIdleS),
  }
}

/** pointer jitter filter: accumulated movement of one pointer since its last counted interaction */
export class JitterFilter {
  private acc = 0
  private lastX = Number.NaN
  private lastY = Number.NaN
  /** true when this move counts as an interaction (pressed, or accumulated >= JITTER_PX) */
  move(x: number, y: number, pressed: boolean): boolean {
    if (Number.isFinite(this.lastX)) this.acc += Math.hypot(x - this.lastX, y - this.lastY)
    this.lastX = x
    this.lastY = y
    if (pressed || this.acc >= JITTER_PX) {
      this.acc = 0
      return true
    }
    return false
  }
  reset(): void {
    this.acc = 0
  }
}
