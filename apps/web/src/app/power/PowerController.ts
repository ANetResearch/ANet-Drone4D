// Power-save controller (R-D2-33, R-D2-34; ADR-121; AWR-04 §15.7; AWR-14 §20.26.4; M15-FR-227, FR-228; UX-FR-152).
// Owner: M15. One singleton installed from main.tsx; it owns the pure PowerMachine (app/power/machine.ts) and performs the
// side effects of its transitions:
//   * interaction detection (AWR-04 §15.7.1): pointerdown, pointermove while pressed or after >= 8 px accumulated, wheel,
//     touchstart, touchmove (a touch drag the browser took over for scrolling sends no pointermove), keydown (a lone modifier
//     only with another key within 2 s), text input (IME, paste, autofill), orientation changes and window resizes of >= 10 %,
//     manual control in progress; listeners are `{passive: true, capture: true}` on window and only write a timestamp; a 1 Hz
//     timer runs the machine while the page is visible (no per-frame work);
//   * external API activity of the sandbox session (issue #59; `sandbox/session.api_activity_age_s` through the sandbox store:
//     SDK, scripts, other clients) restarts the idle clock, cancels the warning and holds off the hidden rule;
//   * visibility: hidden for hidden_s saves; Page Lifecycle `freeze` and a pagehide into the bfcache save at once (the
//     sandbox through the SB-72 park beacon); a pagehide that discards the document (reload, tab close) does not park
//     (D2-AC-38: a reload is back on the same sid; the server parks a closed tab after idle_disconnected_s);
//   * QUIET caps the viewport at 30 fps (phone 20); WARNING shows the countdown bar; SAVING sends a last presence,
//     closes the realtime link (viewer: the shared show's seat is released; sandbox: POST park, then the link closes),
//     renders the last frame and suspends the loop (M06 loop.setSuspended, frames on demand only), freezes the 1 Hz texts
//     and shows the overlay; any input resumes (viewer: reconnect; sandbox: SB-70 resume, possibly a resume ticket); the
//     viewer seats being full (close 4431) puts the viewer into the viewer queue (R81 to R83) with static browsing (input on
//     the canvas renders for 2 s); seat_ready reconnects automatically;
//   * presence (AWR-17 §17.20.4): at most one per presence_min_s per connection, after an interaction, on visibility
//     changes and when entering or leaving power saving; the link sends it (RtProvider; net layer M11);
//   * thresholds: the server's (capacity `power`, serverInfo.power) over the AWR-04 §15.7.2 defaults; test builds accept
//     `?power=viewer|sandbox|off&powerIdle=&powerWarn=&powerQuiet=&powerHidden=` (seconds) for the D2-AC-44 cases.
// Roles: the shared show is `viewer`, the sandbox surface `sandbox`; static browsing, an unavailable sandbox and the local
// build (no seat to reclaim; long benchmark and soak runs must never be suspended or capped) are `none`.
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { DEMO_PUBLIC } from '@/lib/demo'
import { formFactorNow } from '@/lib/formFactor'
import { frameCap } from '@/ui/shell/frameCap'
import { viewport } from '@/viewport/facade'
import { UX } from '@/ui/testing/uxProbe'
import { surfaceStore } from '@/app/shell/surface'
import { sandbox, sandboxStore, principalHint } from '@/ui/sandbox/session'
import { capacityStore } from '@/stores/capacity'
import { manualActive } from '@/ui/manual/controller'
import { timeView } from '@/ui/shell/connView'
import { boot } from '@/app/boot/BootController'
import {
  MODIFIER_WINDOW_MS, POWER_DEFAULTS, PowerMachine, RESIZE_FRAC, JitterFilter, thresholdsFrom, type PowerRole, type PowerState, type PowerThresholds,
  type SaveReason, type Transition,
} from './machine'
import { freezableInterval, setPowerFrozen } from '@/lib/powerFreeze'
import { viewerQueue } from './viewerQueue'
import { PERF_SETTLE_MS, powerStore, type PowerView } from './store'

export type { PowerState, PowerRole, SaveReason } from './machine'
export { powerStore, usePower, type PowerView, type ResumeProgress } from './store'

const VIEWER_SEGS: Readonly<Record<string, number>> = { reconnect: 0.3, state: 0.5, reveal: 0.2 }
const SANDBOX_SEGS: Readonly<Record<string, number>> = { spawn: 0.5, state: 0.3, reveal: 0.2 }
export const RESUME_WEIGHTS = { viewer: VIEWER_SEGS, sandbox: SANDBOX_SEGS } as const

/** presence message (AWR-17 §17.20.4) */
export interface Presence { op: 'presence'; input_age_s: number; visible: boolean; power: 'active' | 'quiet' | 'warning' | 'saving' }
type PresenceSink = (p: Presence) => boolean

let machine = new PowerMachine(Date.now())
let presenceSink: PresenceSink | null = null
let lastPresenceMs = Number.NEGATIVE_INFINITY
let presenceTimer: ReturnType<typeof setTimeout> | null = null
let perfSettleTimer: ReturnType<typeof setTimeout> | null = null
let installed = false
const resumeCbs = new Set<() => void>()
let testOverride: { role?: PowerRole | 'off'; th?: Partial<PowerThresholds> } | null = null

function set(p: Partial<PowerView>): void {
  const s = powerStore.getState()
  powerStore.setState({ ...p, version: s.version + 1 })
}

const visible = (): boolean => typeof document === 'undefined' || document.visibilityState !== 'hidden'

/** the frame-interval figures pause with the loop (on) and settle PERF_SETTLE_MS after the page is live again (off) */
function perfPause(on: boolean): void {
  if (perfSettleTimer) clearTimeout(perfSettleTimer)
  perfSettleTimer = null
  if (on) {
    if (!powerStore.getState().perfPaused) set({ perfPaused: true })
    return
  }
  if (!powerStore.getState().perfPaused) return
  perfSettleTimer = setTimeout(() => {
    perfSettleTimer = null
    if (powerStore.getState().state === 'ACTIVE' || powerStore.getState().state === 'QUIET' || powerStore.getState().state === 'WARNING') set({ perfPaused: false })
  }, PERF_SETTLE_MS)
}

// ------------------------------------------------------------ role and thresholds
function roleNow(): PowerRole {
  if (testOverride?.role === 'off') return 'none'
  if (testOverride?.role) return testOverride.role
  const s = surfaceStore.getState()
  if (s.surface === 'sandbox') return 'sandbox'
  if (s.surface === 'shared') return 'viewer'
  if (s.surface === 'local' && DEMO_PUBLIC) return 'viewer'
  return 'none'
}

function busyNow(role: PowerRole): boolean {
  const d = capacityStore.getState().data
  if (!d) return false
  if (role === 'sandbox') return (d.sandbox.queue ?? 0) > 0
  return d.viewers.state === 'full' && (d.viewers.queue ?? 0) > 0
}

/** server thresholds (capacity `power`, serverInfo.power) with the test override on top */
let serverPower: Record<string, unknown> | null = null
function applyThresholds(): void {
  let th = thresholdsFrom({ ...POWER_DEFAULTS }, capacityStore.getState().data?.power as unknown as Record<string, unknown> | undefined)
  th = thresholdsFrom(th, serverPower)
  if (testOverride?.th) th = { ...th, ...testOverride.th }
  machine.th = th
  const cur = powerStore.getState().thresholds
  if (JSON.stringify(cur) !== JSON.stringify(th)) set({ thresholds: th })
}

function syncContext(): void {
  const role = roleNow()
  const internal = surfaceStore.getState().internal
  machine.ctx = { role, busy: busyNow(role), internal, keepRunning: powerStore.getState().keepRunning }
  if (powerStore.getState().role !== role) set({ role })
}

// ------------------------------------------------------------ presence
function presencePower(st: PowerState): Presence['power'] {
  return st === 'QUIET' ? 'quiet' : st === 'WARNING' ? 'warning' : st === 'SAVING' ? 'saving' : 'active'
}

/** send now if the minimum interval allows, else once at the next allowed time */
function presence(force = false): void {
  if (!presenceSink || machine.ctx.role === 'none') return
  const now = Date.now()
  const minMs = machine.th.presenceMinS * 1000
  const due = lastPresenceMs + minMs
  if (now < due && !force) {
    if (!presenceTimer) presenceTimer = setTimeout(() => {
      presenceTimer = null
      presence()
    }, due - now)
    return
  }
  if (now < due) return
  const ok = presenceSink({ op: 'presence', input_age_s: Math.round(machine.inputAgeS(now) * 10) / 10, visible: visible(), power: presencePower(machine.state) })
  if (ok) {
    lastPresenceMs = now
    UX.power.presence++
  }
}

// ------------------------------------------------------------ transitions and their effects
function record(t: Transition): void {
  UX.power.state = t.to
  UX.power.reason = machine.reason ?? ''
  UX.power.transitions.push(`${t.from}>${t.to}:${t.reason}`)
  if (UX.power.transitions.length > 64) UX.power.transitions.shift()
}

function quietCap(on: boolean): void {
  frameCap.set('power-quiet', on ? (formFactorNow().device === 'phone' ? 20 : 30) : 0)
}

function enterSaving(reason: SaveReason, beacon: boolean): void {
  const role = machine.ctx.role
  presence(true)
  if (presenceTimer) clearTimeout(presenceTimer)
  presenceTimer = null
  quietCap(false)
  const simMs = timeView.tSimMs
  set({ state: 'SAVING', reason, warnLeftS: 0, frozen: true, linkOpen: false, lastUpdateMs: Date.now(), lastSimNs: Number.isFinite(simMs) ? Math.round(simMs * 1e6) : null, resume: null })
  if (role === 'viewer') void viewerQueue.leave()
  if (role === 'sandbox') {
    if (reason === 'server') sandbox.markParked('idle')
    else if (beacon) sandbox.parkBeacon('hidden')
    else void sandbox.park(reason === 'hidden' ? 'hidden' : 'idle')
  }
  // the last frame is on screen; from now on frames are rendered on demand only (overlay first paint, resize, theme), and
  // the periodic UI work (4 Hz red arbitration and event flush, 1 Hz clocks, chart frames) stops (D2-AC-44)
  frameCap.suspend('power', true)
  setPowerFrozen(true)
  perfPause(true)
  viewport.requestFrame()
}

async function enterResuming(): Promise<void> {
  const role = machine.ctx.role
  frameCap.suspend('power', false)
  setPowerFrozen(false)
  viewport.setWakeOnInput(false)
  quietCap(false)
  const kind = role === 'sandbox' ? 'sandbox' : 'viewer'
  const seg = Object.fromEntries(Object.keys(RESUME_WEIGHTS[kind]).map((k) => [k, 0]))
  perfPause(true)
  set({ state: 'RESUMING', frozen: true, resume: { kind, seg, queued: false } })
  for (const cb of resumeCbs) cb()
  if (role === 'viewer') {
    set({ linkOpen: true })
    return
  }
  if (role === 'sandbox') {
    const r = await sandbox.resumeParked()
    if (r === 'queued') apply(machine.queued())
    else if (r === 'active') resumeSeg('spawn', 1)
    else if (r === 'resumed') resumeSeg('spawn', 0.4)
    else if (r === 'ended' || r === 'failed') {
      // the ended dialog (D2) or the "parked" overlay stays: back to a state without a link
      apply(machine.live())
      set({ frozen: false, resume: null })
    }
    set({ linkOpen: true })
  }
}

function enterQueued(): void {
  const role = machine.ctx.role
  set({ state: 'QUEUED', frozen: true, linkOpen: role !== 'viewer' })
  setPowerFrozen(true)
  perfPause(true)
  const r = powerStore.getState().resume
  if (r) set({ resume: { ...r, queued: true } })
  if (role === 'viewer') {
    // static browsing while queued: the last state, camera input renders for 2 s (AWR-04 §15.7.7 item 3)
    frameCap.suspend('power', true)
    viewport.setWakeOnInput(true)
    viewport.requestFrame()
    void viewerQueue.join(principalHint(), () => apply(machine.ready()))
  }
}

function enterActive(from: PowerState): void {
  quietCap(false)
  setPowerFrozen(false)
  if (from === 'RESUMING' || from === 'QUEUED' || from === 'SAVING') {
    frameCap.suspend('power', false)
    viewport.setWakeOnInput(false)
  }
  set({ state: 'ACTIVE', reason: null, warnLeftS: 0, frozen: false, linkOpen: true, resume: null })
  perfPause(false)
  presence(true)
}

/** apply one transition of the machine (null: nothing) */
function apply(t: Transition | null, beacon = false): void {
  if (!t) return
  record(t)
  switch (t.to) {
    case 'QUIET':
      quietCap(true)
      set({ state: 'QUIET', warnLeftS: 0 })
      break
    case 'WARNING':
      set({ state: 'WARNING', warnLeftS: machine.warnLeftS(Date.now()) })
      presence()
      break
    case 'ACTIVE':
      enterActive(t.from)
      break
    case 'SAVING':
      enterSaving(machine.reason ?? 'idle', beacon)
      break
    case 'RESUMING':
      if (t.from === 'QUEUED' && machine.ctx.role === 'viewer') {
        frameCap.suspend('power', false)
        setPowerFrozen(false)
        viewport.setWakeOnInput(false)
        set({ state: 'RESUMING', linkOpen: true })
        break
      }
      void enterResuming()
      break
    case 'QUEUED':
      enterQueued()
      break
  }
}

// ------------------------------------------------------------ interaction detection (AWR-04 §15.7.1)
const jitter = new JitterFilter()
let modifierAtMs = Number.NEGATIVE_INFINITY
let lastSize = { w: 0, h: 0 }
const MODIFIERS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'CapsLock'])

function onInput(): void {
  const before = machine.state
  const t = machine.input(Date.now())
  apply(t)
  if (before === 'ACTIVE' || before === 'QUIET' || before === 'WARNING') presence()
}

function onPointerDown(): void {
  jitter.reset()
  onInput()
}
function onPointerMove(e: PointerEvent): void {
  if (jitter.move(e.clientX, e.clientY, e.buttons !== 0)) onInput()
}
function onKey(e: KeyboardEvent): void {
  if (MODIFIERS.has(e.key)) {
    modifierAtMs = Date.now()
    return
  }
  modifierAtMs = Number.NEGATIVE_INFINITY
  onInput()
}
function onResize(): void {
  const w = globalThis.innerWidth || 0
  const h = globalThis.innerHeight || 0
  const big = lastSize.w > 0 && (Math.abs(w - lastSize.w) / lastSize.w >= RESIZE_FRAC || Math.abs(h - lastSize.h) / Math.max(1, lastSize.h) >= RESIZE_FRAC)
  lastSize = { w, h }
  if (big) onInput()
  if (machine.state === 'SAVING') viewport.requestFrame()
}
function onVisibility(): void {
  const v = visible()
  machine.visibility(v, Date.now())
  presence(true)
  if (v && machine.state === 'SAVING') viewport.requestFrame()
}
/** an in-app reload follows (world switch, update banner, retry): pagehide neither saves nor parks the sandbox (D2-INT: the
 *  world-switch reload used to park the session through the SB-72 beacon and the reloaded page waited in "parking") */
let navigating = false

/**
 * pagehide and Page Lifecycle freeze. A page that stays alive (bfcache, `persisted`; a frozen background tab) saves at once
 * and parks the sandbox through the SB-72 beacon. A document that is being discarded (reload, navigation away, tab close;
 * `persisted = false`) does neither (D2-FX-R2-web, D2-AC-38): the beacon used to park the sandbox on every reload, and the
 * reloaded page then waited for the resume from the checkpoint (3.5 s, often behind the shader zoo under the boot mask:
 * 14-15 s) instead of being back on the same sid within 3 s; the unloading document closes its sockets anyway, and a
 * closed tab is parked by the server's no-connection rule (sandbox.park.idle_disconnected_s 120 s, 60 s with a queue,
 * AWR-04 §15.7.2), the viewer's seat is released by the closed socket.
 */
function onPageHide(e?: Event): void {
  if (navigating) return
  if (boot.state !== 'REVEALED' && boot.state !== 'BOOT_ERROR') return // 遮罩期间不进入节能（D2-AC-44）
  if (e?.type === 'pagehide' && (e as PageTransitionEvent).persisted !== true) return
  apply(machine.pageHide(), true)
}

function tick(): void {
  syncContext()
  applyThresholds()
  if (manualActive()) machine.input(Date.now())
  // a modifier alone counts once its window passed without another key (AWR-04 §15.7.1 keyboard row: it does not)
  if (Date.now() - modifierAtMs > MODIFIER_WINDOW_MS) modifierAtMs = Number.NEGATIVE_INFINITY
  const t = machine.tick(Date.now())
  apply(t)
  if (machine.state === 'WARNING') {
    const left = machine.warnLeftS(Date.now())
    if (left !== powerStore.getState().warnLeftS) set({ warnLeftS: left })
  }
  UX.power.frames = viewport.suspended ? 1 : 0
}

// ------------------------------------------------------------ public interface
export const power = {
  /** the page reloads or navigates on purpose right after this call (see `navigating`) */
  expectNavigation(): void {
    navigating = true
  },
  /** the link reports its state (RtProvider): LIVE completes a resume, 4430 is the server's reclaim, 4431 the seats */
  onConn(state: string, code: number | null): void {
    UX.power.conn = `${state}${code ? `:${code}` : ''}`
    if (code === 4430 && state !== 'LIVE' && state !== 'DEGRADED') {
      apply(machine.serverReclaimed())
      return
    }
    if (code === 4431 && state !== 'LIVE' && state !== 'DEGRADED') {
      if (machine.state !== 'QUEUED') apply(machine.queued())
      else set({ linkOpen: false })
      return
    }
    const r = powerStore.getState().resume
    if (machine.state === 'RESUMING' && r) {
      if (state === 'SYNCING') resumeSeg(r.kind === 'viewer' ? 'reconnect' : 'spawn', 1)
      if (state === 'LIVE' || state === 'DEGRADED') {
        resumeSeg(r.kind === 'viewer' ? 'reconnect' : 'spawn', 1)
        resumeSeg('state', 1)
        resumeSeg('reveal', 1)
      }
    }
    if ((state === 'LIVE' || state === 'DEGRADED') && (machine.state === 'RESUMING' || machine.state === 'QUEUED')) apply(machine.live())
  },
  /** serverInfo.power (AWR-17 §17.20.4) */
  setServerPower(p: Record<string, unknown> | null): void {
    serverPower = p
    applyThresholds()
  },
  /** the sink that sends presence on the page client (RtProvider; false when the link cannot send it) */
  setPresenceSink(s: PresenceSink | null): void {
    presenceSink = s
  },
  /** internal identity: never warn or save (presence reports input_age 0) */
  setKeepRunning(on: boolean): void {
    set({ keepRunning: on })
    syncContext()
    if (on && (machine.state === 'WARNING' || machine.state === 'QUIET')) apply(machine.input(Date.now()))
  },
  /** callbacks when a resume starts (stores refresh on the first SNAPSHOT anyway) */
  onResume(cb: () => void): () => void {
    resumeCbs.add(cb)
    return () => {
      resumeCbs.delete(cb)
    }
  },
  /** an explicit interaction (overlay buttons) */
  input(): void {
    onInput()
  },
  get state(): PowerState {
    return machine.state
  },
  /** tests: the machine and an immediate tick */
  get machine(): PowerMachine {
    return machine
  },
  tickNow(): void {
    tick()
  },
}

/** the sandbox resume progressed (spawn on 201/200, state and reveal on LIVE) */
function resumeSeg(id: string, v: number): void {
  const r = powerStore.getState().resume
  if (!r || (r.seg[id] ?? 0) >= v) return
  set({ resume: { ...r, seg: { ...r.seg, [id]: v } } })
}

function parseTestOverride(): void {
  if (!TEST_SWITCHES || typeof location === 'undefined') return
  const q = new URLSearchParams(location.search)
  const role = q.get('power')
  const num = (k: string): number | undefined => {
    const v = Number(q.get(k))
    return q.has(k) && Number.isFinite(v) && v > 0 ? v : undefined
  }
  const th: Partial<PowerThresholds> = {}
  const idle = num('powerIdle')
  if (idle !== undefined) {
    th.viewerIdleS = idle
    th.viewerIdleBusyS = idle
    th.sandboxIdleS = idle
    th.sandboxIdleBusyS = idle
  }
  const warn = num('powerWarn')
  if (warn !== undefined) th.warnS = warn
  const quiet = num('powerQuiet')
  if (quiet !== undefined) th.quietS = quiet
  const hidden = num('powerHidden')
  if (hidden !== undefined) th.hiddenS = hidden
  const pmin = num('powerPresence')
  if (pmin !== undefined) th.presenceMinS = pmin
  if (role === 'viewer' || role === 'sandbox' || role === 'off' || Object.keys(th).length) {
    testOverride = { role: role === 'viewer' || role === 'sandbox' || role === 'off' ? role : undefined, th }
  }
}

/** start-up (main.tsx): idempotent; returns the uninstall (tests) */
export function installPowerController(): () => void {
  if (installed || typeof window === 'undefined') return () => {}
  installed = true
  machine = new PowerMachine(Date.now())
  parseTestOverride()
  lastSize = { w: window.innerWidth || 0, h: window.innerHeight || 0 }
  const o = { passive: true, capture: true } as const
  window.addEventListener('pointerdown', onPointerDown, o)
  window.addEventListener('pointermove', onPointerMove, o)
  window.addEventListener('wheel', onInput, o)
  window.addEventListener('touchstart', onInput, o)
  window.addEventListener('touchmove', onInput, o)
  window.addEventListener('keydown', onKey, o)
  window.addEventListener('input', onInput, o)
  window.addEventListener('orientationchange', onInput, o)
  window.addEventListener('resize', onResize, o)
  window.addEventListener('pagehide', onPageHide, o)
  document.addEventListener('visibilitychange', onVisibility, o)
  document.addEventListener('freeze', onPageHide, o)
  machine.visibility(visible(), Date.now())
  syncContext()
  applyThresholds()
  const offSurface = surfaceStore.subscribe(() => syncContext())
  const offCap = capacityStore.subscribe(() => applyThresholds())
  // a sandbox that resumed reaches LIVE through the new link; a sandbox ended while parked leaves the overlay
  const offSb = sandboxStore.subscribe((s, p) => {
    if (s.sid !== p.sid) machine.activityMs = Number.NEGATIVE_INFINITY
    if (s.apiActivityMs !== p.apiActivityMs && s.apiActivityMs > 0 && machine.ctx.role === 'sandbox') apply(machine.activity(s.apiActivityMs, Date.now()))
    if (s.phase === p.phase) return
    if (machine.state === 'QUEUED' && machine.ctx.role === 'sandbox' && (s.phase === 'spawning' || s.phase === 'ready' || s.phase === 'active')) {
      apply(machine.ready())
      resumeSeg('spawn', 0.4)
    }
    if (machine.state === 'RESUMING' && machine.ctx.role === 'sandbox' && (s.phase === 'ready' || s.phase === 'active')) resumeSeg('spawn', 1)
    if (s.phase === 'ended' && machine.state !== 'ACTIVE') {
      apply(machine.live())
      set({ frozen: false, resume: null })
    }
  })
  // nobody can interact under the boot mask: the idle clock starts at the reveal
  const offBoot = boot.subscribe((st) => {
    if (st !== 'REVEALED') return
    machine.lastInputMs = Date.now()
    if (!visible()) machine.visibility(true, Date.now()) // 遮罩期间的隐藏不计入 hidden_s（揭开时重新起算）
    machine.visibility(visible(), Date.now())
  })
  // the 1 Hz check is frozen with the rest of the periodic UI work while saving or queued (machine.tick makes no transition
  // there; leaving those states is event driven: input, link state, seat_ready and slot_ready), so the power-save mode
  // keeps no timer of its own (d2-FX-R2-sandbox-to-M15 item 1, D2-AC-44, D2-AC-46)
  const stopTick = freezableInterval(() => {
    // D2-INT（d2-WP-13b-to-M15 第 3 条，D2-AC-44）：遮罩可见期间不计时、不进入 QUIET/WARNING/SAVING（慢设备或后台标签页加载时
    // 揭开可能晚于阈值）；揭开时 offBoot 重置最近交互时刻
    if (boot.state !== 'REVEALED' && boot.state !== 'BOOT_ERROR') {
      machine.lastInputMs = Date.now()
      return
    }
    if (visible() || machine.canSave()) tick()
  }, 1000)
  UX.power.state = machine.state
  return () => {
    installed = false
    window.removeEventListener('pointerdown', onPointerDown, o)
    window.removeEventListener('pointermove', onPointerMove, o)
    window.removeEventListener('wheel', onInput, o)
    window.removeEventListener('touchstart', onInput, o)
    window.removeEventListener('touchmove', onInput, o)
    window.removeEventListener('keydown', onKey, o)
    window.removeEventListener('input', onInput, o)
    window.removeEventListener('orientationchange', onInput, o)
    window.removeEventListener('resize', onResize, o)
    window.removeEventListener('pagehide', onPageHide, o)
    document.removeEventListener('visibilitychange', onVisibility, o)
    document.removeEventListener('freeze', onPageHide, o)
    offSurface()
    offCap()
    offSb()
    offBoot()
    stopTick()
    if (perfSettleTimer) clearTimeout(perfSettleTimer)
    perfSettleTimer = null
    setPowerFrozen(false)
  }
}
