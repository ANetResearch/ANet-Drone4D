// Transport control logic of the Timeline (M12 §6.5; FR-014 to FR-016, FR-022, FR-025, FR-046, FR-047; AWR-14 §6.17).
// Owner: M12. Pure functions and small state machines, no network: stores/timeline.ts sends the calls.
//   PendingMachine  IDLE -> PENDING on a user action; back to IDLE when TIME reaches the target (state, rate, t_sim),
//                   when the call is rejected, after 1 s without confirmation (revert and notify), or on a new epoch.
//   guards()        one table drives both disabling and the tooltip reason (UI never hard-codes clock capabilities).
//   step targets    live: ticks per step kind; replay: seek targets on the 1 s / 10 s / block grid.
//   RtfWatch        actual < 0.95 x requested for 2 s shows the RTF badge; 2 s of recovery hides it.
import { TIME_PARAMS as P, TS, TICK_MS } from './params'

export type Control = 'play' | 'pause' | 'speed' | 'step' | 'seek'
export type StepKind = 'tick' | '100ms' | '1s' | 'sample' | '-sample' | '-1s' | '10s' | '-10s'
export interface Pending {
  control: Control
  /** target state4 (play, pause), rate (speed), t_sim ms (step, seek) */
  target: number
  deadlineMs: number
  /** state before the action (revert) */
  fromState4: number
  fromRate: number
  fromTSimMs: number
}
export type PendingOutcome = 'confirmed' | 'timeout' | 'epoch' | null

export class PendingMachine {
  pending: Pending | null = null
  private epoch = -1

  start(control: Control, target: number, nowMs: number, from: { state4: number; rate: number; tSimMs: number; epoch: number }): Pending {
    this.pending = { control, target, deadlineMs: nowMs + P.pendingTimeoutMs, fromState4: from.state4, fromRate: from.rate, fromTSimMs: from.tSimMs }
    this.epoch = from.epoch
    return this.pending
  }

  /** the call was answered: succeeded confirms rate and step controls, rejected drops the pending state */
  settle(ok: boolean): Pending | null {
    const p = this.pending
    if (!p) return null
    if (!ok || p.control === 'speed' || p.control === 'step') this.pending = null
    return p
  }

  /** a replay seek was answered (playbackState{did_seek} or an error) */
  seekDone(): void {
    if (this.pending?.control === 'seek') this.pending = null
  }

  /** evaluate against the latest TIME and wall clock */
  observe(state4: number, rate: number, tSimMs: number, epoch: number, nowMs: number): PendingOutcome {
    const p = this.pending
    if (!p) return null
    if (epoch !== this.epoch && p.control !== 'seek') {
      this.pending = null
      return 'epoch'
    }
    let ok = false
    switch (p.control) {
      case 'play':
        ok = state4 === TS.PLAYING || state4 === TS.LIVE || (state4 === TS.BUFFERING && p.fromState4 !== TS.BUFFERING)
        break
      case 'pause':
        ok = state4 === TS.PAUSED
        break
      case 'speed':
        ok = Math.abs(rate - p.target) <= 0.01 * p.target
        break
      case 'step':
        ok = state4 === TS.PAUSED && tSimMs >= p.target - 0.5
        break
      case 'seek':
        ok = false
        break
    }
    if (ok) {
      this.pending = null
      return 'confirmed'
    }
    if (nowMs >= p.deadlineMs && p.control !== 'seek') {
      this.pending = null
      return 'timeout'
    }
    return null
  }

  clear(): void {
    this.pending = null
  }
}

export interface GuardInput {
  mode: 'live' | 'replay'
  /** operator or admin holding the write seat */
  canWrite: boolean
  caps: { pausable: boolean; steppable: boolean; maxSpeed: number }
  state4: number
  playbackStatus: string | null
  speedMax: number
}
export interface Guards {
  /** null when allowed, else an i18n reason key */
  play: string | null
  step: string | null
  speed(rate: number): string | null
  seek: string | null
}

const BLOCKED = new Set<number>([TS.STALLED, TS.RESTARTING, TS.FAILED, TS.LIVE])
const REPLAY_OPEN = new Set(['paused', 'playing', 'ended', 'buffering'])

/** M12 §6.5 guard table */
export function guards(g: GuardInput): Guards {
  if (g.mode === 'replay') {
    const base = !g.canWrite ? 'hint.replaySeat' : !REPLAY_OPEN.has(g.playbackStatus ?? '') ? 'hint.replayNotOpen' : null
    return {
      play: base,
      step: base,
      seek: base,
      speed: (r) => base ?? (r > g.speedMax + 1e-9 ? 'hint.replaySpeedMax' : null),
    }
  }
  const seat = g.canWrite ? null : 'hint.needSeat'
  const blocked = g.state4 === TS.LIVE ? 'hint.clockLive' : BLOCKED.has(g.state4) ? `time.state.${g.state4}` : null
  const play = seat ?? (!g.caps.pausable ? 'hint.clockLocked' : blocked)
  const step = play ?? (!g.caps.steppable ? 'hint.clockLocked' : g.state4 !== TS.PAUSED ? 'hint.pauseFirst' : null)
  return {
    play,
    step,
    seek: 'hint.liveNoRewind',
    speed: (r) => seat ?? (g.state4 === TS.LIVE ? 'hint.clockLive' : BLOCKED.has(g.state4) ? `time.state.${g.state4}` : r > g.caps.maxSpeed + 1e-9 ? 'hint.maxSpeed' : null),
  }
}

/** live step kinds -> sim/step ticks (-> 25, Shift+-> 250, Shift+. 1; 4 ms per tick) */
export const LIVE_STEP_TICKS: Readonly<Partial<Record<StepKind, number>>> = { tick: 1, '100ms': 25, '1s': 250 }
export const liveStepMs = (k: StepKind): number => (LIVE_STEP_TICKS[k] ?? 0) * TICK_MS

/** the live hotkeys keep their keys in replay: -> is +1 s, Shift+-> +10 s, Shift+. one block (14 §6.10) */
const REPLAY_ALIAS: Readonly<Partial<Record<StepKind, StepKind>>> = { tick: 'sample', '100ms': '1s', '1s': '10s' }
export const replayStepKind = (k: StepKind): StepKind => REPLAY_ALIAS[k] ?? k

/** replay seek target (s) of a step from tS, clamped to [startS, endS]; the block steps align to the block grid */
export function replayStepTarget(kind: StepKind, tS: number, blockS: number, startS: number, endS: number): number {
  const k = replayStepKind(kind)
  const b = blockS > 0 ? blockS : P.blockIntervalMs / 1000
  let t = tS
  switch (k) {
    case '1s':
      t = tS + 1
      break
    case '-1s':
      t = tS - 1
      break
    case '10s':
      t = tS + 10
      break
    case '-10s':
      t = tS - 10
      break
    case 'sample': {
      const g = Math.floor(tS / b + 1e-6) * b
      t = g + b
      break
    }
    case '-sample': {
      const g = Math.ceil(tS / b - 1e-6) * b
      t = g - b
      break
    }
    default:
      break
  }
  // round to the ns grid of the wire (seek_ns is an integer)
  t = Math.round(t * 1e9) / 1e9
  return Math.min(endS, Math.max(startS, t))
}

/** RTF badge with 2 s hold in both directions */
export class RtfWatch {
  limited = false
  private since = Number.NaN

  update(actual: number, requested: number, advancing: boolean, nowMs: number): boolean {
    const below = advancing && requested > 0 && actual < P.rtfRatio * requested
    if (below !== this.limited) {
      if (Number.isNaN(this.since)) this.since = nowMs
      if (nowMs - this.since >= P.rtfHoldMs) {
        this.limited = below
        this.since = Number.NaN
      }
    } else this.since = Number.NaN
    return this.limited
  }
  reset(): void {
    this.limited = false
    this.since = Number.NaN
  }
}

/** reason codes of a rejected clock or playback call -> i18n key (AWR-14 §13.6; FR-014) */
export function reasonKey(code: number): string {
  switch (code) {
    case 115:
      return 'reason.115'
    case 116:
      return 'reason.116'
    case 117:
      return 'reason.117'
    case 118:
      return 'reason.118'
    case 110:
      return 'reason.110'
    case 111:
      return 'reason.111'
    case 122:
      return 'reason.122'
    case 211:
    case 213:
      return 'reason.213'
    default:
      return `reason.${code}`
  }
}
