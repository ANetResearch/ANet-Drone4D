// SimClockView (M12 §6.3, FR-001, FR-002, FR-004, FR-005, FR-013; AWR-17 §6.10; ADR-045, ADR-046). Owner: M12.
// The client's view of the authoritative SimClock (M08) built from TIME only: state4 = state & 0x0F (bit 7 REPLAY
// masked; unknown values count as STALLED), epochs compared for equality only (u16). simNow advances only in PLAYING and
// LIVE: raw = t_sim + rate × (srvNow − t_srv) capped at t_sim + rate × 1 s; epoch or state changes, frozen states and
// |raw − prediction| > 0.25 s × max(rate, 1) snap, otherwise simNow converges with τ = 250 ms and never decreases.
// TIME older than 1 s (wall) marks the view stale. tRender = simNow − D_sim (DelayController), tFocus = simNow −
// D_focus. While STEPPING the seen time holds; STEPPING -> PAUSED with a step <= 1 s glides over --duration-fast
// (StepGlide). One call per frame from the clock phase; no allocation.
import { DelayController } from './delay'
import { epochBus } from './epochBus'
import { TIME_PARAMS as P, TS, advancesIn } from './params'
import { perfTime } from './perfTime'
import { StepGlide } from './stepGlide'

export { DelayController } from './delay'

/** decoded TIME (M11 TimeFrameView, AWR-17 §6.4): ms from the run start and from gw_t0 */
export interface TimeFrame { state: number; epoch: number; rate: number; tSimMs: number; tSrvMs: number }

/** read-only clock interface for consumers (M12 §7.1) */
export interface ClockView {
  simNowS(): number
  tRenderS(): number
  tFocusS(): number
  readonly rate: number
  readonly state4: number
  readonly replay: boolean
  readonly stale: boolean
  readonly dGlobalMs: number
  readonly dFocusMs: number
  readonly focusLowLatency: boolean
}

export class SimClockView implements ClockView {
  private baseSimMs = 0
  private baseSrvMs = 0
  /** TIME.rate (actual rate; 0 in some frozen states) */
  rate = 1
  /** last rate seen while advancing (for wall-clock ages in frozen states) */
  rateNominal = 1
  state4: number = TS.STOPPED
  replay = false
  stale = true
  epoch = -1
  /** TIME.t_sim of the latest TIME (ms) */
  tSimMs = 0
  private simMs = 0
  private lastNowMs = Number.NaN
  private lastTimeRecvMs = Number.NEGATIVE_INFINITY
  private snapPending = true
  private holdStep = false
  private lastRenderMs = 0
  /** true once a TIME was received */
  hasTime = false
  clockSnaps = 0
  unknownState = 0
  /** motion tier reduced (set by the runtime): no freeze decay, no step glide */
  reduced = false
  readonly delay = new DelayController()
  readonly glide = new StepGlide()

  /** TIME (object form, M12 §7.1) received at recvMs (main-thread time base) */
  onTime(t: TimeFrame, recvMs: number): void {
    this.onTimeFields(t.state, t.epoch, t.rate, t.tSimMs, t.tSrvMs, recvMs)
  }

  /** TIME fields from the TelemetryFrame header (zero allocation) */
  onTimeFields(stateByte: number, epoch: number, rate: number, tSimMs: number, tSrvMs: number, recvMs: number): void {
    let s4 = stateByte & 0x0f
    if (s4 > TS.LIVE) {
      s4 = TS.STALLED
      this.unknownState++
      perfTime.unknownState = this.unknownState
    }
    const e = epoch & 0xffff
    const prev4 = this.state4
    const prevT = this.tSimMs
    this.holdStep = false
    if (e !== this.epoch) {
      this.epoch = e
      this.snapPending = true
      this.glide.cancel()
      this.delay.onEpoch()
      epochBus.emitEpoch(e)
    } else if (s4 !== prev4 || !advancesIn(s4)) {
      // frozen step: STEPPING holds the seen time; PAUSED after a step of <= 1 s glides to the new t_sim
      const stepping = prev4 === TS.PAUSED || prev4 === TS.STEPPING
      if (s4 === TS.STEPPING && stepping) this.holdStep = true
      else if (s4 === TS.PAUSED && stepping && tSimMs !== prevT) {
        const from = this.glide.active ? this.glide.toMs : this.lastRenderMs
        const d = tSimMs - from
        if (d > 0 && d <= P.glideMaxStepMs) this.glide.request(from, tSimMs, this.reduced)
        else this.glide.cancel()
      }
      if (s4 !== prev4) this.snapPending = true
    }
    this.baseSimMs = tSimMs
    this.baseSrvMs = tSrvMs
    this.tSimMs = tSimMs
    this.rate = rate
    if (advancesIn(s4) && rate > 0) this.rateNominal = rate
    this.state4 = s4
    this.replay = (stateByte & 0x80) !== 0
    this.lastTimeRecvMs = recvMs
    this.hasTime = true
  }

  get advancing(): boolean {
    return advancesIn(this.state4)
  }

  /** clock phase: nowMs main-thread time, srvNowMs = nowMs + clockOffsetMainMs (NaN before the first pong) */
  tick(nowMs: number, srvNowMs: number): void {
    const adv = this.advancing
    let raw = this.baseSimMs
    if (adv && Number.isFinite(srvNowMs)) {
      raw = Math.min(this.baseSimMs + this.rate * (srvNowMs - this.baseSrvMs), this.baseSimMs + this.rate * P.extrapCapMs)
      raw = Math.max(raw, this.baseSimMs - this.rate * P.extrapCapMs)
    }
    const dt = Number.isNaN(this.lastNowMs) ? 0 : Math.max(0, nowMs - this.lastNowMs)
    this.lastNowMs = nowMs
    if (!adv) {
      if (!this.holdStep) this.simMs = raw
      this.snapPending = false
    } else if (this.snapPending) {
      this.simMs = raw
      this.snapPending = false
    } else {
      const pred = this.simMs + this.rate * dt
      const err = raw - pred
      if (Math.abs(err) > P.snapMs * Math.max(this.rate, 1)) {
        this.simMs = raw
        this.clockSnaps++
      } else this.simMs = Math.max(this.simMs, pred + err * (1 - Math.exp(-dt / P.convergeTauMs)))
    }
    this.stale = nowMs - this.lastTimeRecvMs > P.staleMs
    this.delay.tick(nowMs, dt, adv, this.rate, this.reduced)
    this.lastRenderMs = this.glide.active ? this.glide.value(nowMs) - this.delay.dSimMs : this.simMs - this.delay.dSimMs
  }

  simNowS(): number {
    return this.simMs / 1000
  }
  tRenderS(): number {
    return this.lastRenderMs / 1000
  }
  tFocusS(): number {
    if (this.glide.active) return this.tRenderS()
    return (this.simMs - this.delay.dFocusSimMs) / 1000
  }
  get dGlobalMs(): number {
    return this.delay.dSimMs
  }
  get dFocusMs(): number {
    return this.delay.dFocusSimMs
  }
  get focusLowLatency(): boolean {
    return this.delay.focusLowLatency
  }
  /** wall-clock ms since the last TIME */
  timeAgeMs(nowMs: number): number {
    return nowMs - this.lastTimeRecvMs
  }
}
