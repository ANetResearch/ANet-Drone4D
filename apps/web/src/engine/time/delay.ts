// DelayController (M12 §6.3, FR-003, FR-004, FR-005; ADR-046 and its round-2 amendment). Owner: M12.
// Wall-clock part: D_wall = clamp(2 / hz_eff + jitter_p95, 60, 300 ms) where hz_eff counts new swarm samples in a 1 s
// window (EMA) and jitter_p95 is the p95 of |Δw − 1/hz_eff| over the last 32 arrival intervals; D_wall follows its
// target with a 1 s time constant, at most 10 %/s, with 5 % hysteresis (the rate limit binds D_wall only). The first
// valid measurement initialises D_wall directly (no 4 s crawl down from the 300 ms prior).
// Simulation part D_sim = rate × D_wall with three rules: frozen (not PLAYING or LIVE) decays D_sim from its value at
// the freeze to exactly 0 over --duration-quick with --ease-smooth-out (reduced motion: 0 at once) and stops updating
// hz_eff and jitter; resuming and a new epoch ramp D_sim linearly over 1 s from its current value (0) to the target; a
// rate change ramps linearly over 1 s from the value at the change. tRender = simNow − D_sim stays monotone because the
// ramp slope is at most 0.3 s × rate_new per second (M12 §6.3 proof). The focus exception (Third/FPV) runs the same rules
// on its own channel (60 Hz focus samples) and blends in and out over 300 ms. No allocation.
import { EASE, MOTION } from '@/lib/tokens/motion.gen'
import { bezierAt } from '../anim/bezier'
import { TIME_PARAMS as P } from './params'

/** arrival statistics of one channel (swarm or focus): hz_eff and jitter_p95 (wall-clock ms) */
export class ArrivalStats {
  hzEff = 0
  jitterMs = 0
  private readonly arr = new Float64Array(P.hzArrivals)
  private nArr = 0
  private headArr = 0
  private readonly dw = new Float64Array(P.jitterSamples)
  private nDw = 0
  private headDw = 0
  private readonly tmp = new Float64Array(P.jitterSamples)
  private last = Number.NaN

  onSample(recvMs: number): void {
    if (Number.isFinite(this.last)) {
      const d = recvMs - this.last
      if (d <= 0) return // same arrival (several slots from one sample) or clock step back
      this.dw[this.headDw] = d
      this.headDw = (this.headDw + 1) % P.jitterSamples
      if (this.nDw < P.jitterSamples) this.nDw++
    }
    this.last = recvMs
    this.arr[this.headArr] = recvMs
    this.headArr = (this.headArr + 1) % P.hzArrivals
    if (this.nArr < P.hzArrivals) this.nArr++
    // hz over the arrivals in (recvMs - 1 s, recvMs]
    let c = 0
    let oldest = recvMs
    for (let k = 0; k < this.nArr; k++) {
      const a = this.arr[(this.headArr - 1 - k + P.hzArrivals) % P.hzArrivals]
      if (recvMs - a >= P.hzWindowMs) break
      c++
      oldest = a
    }
    let hz = 0
    if (c >= 2 && recvMs > oldest) hz = ((c - 1) * 1000) / (recvMs - oldest)
    else if (this.nDw > 0) hz = 1000 / this.dw[(this.headDw - 1 + P.jitterSamples) % P.jitterSamples]
    if (!(hz > 0)) return
    this.hzEff = this.hzEff > 0 ? this.hzEff + P.hzEma * (hz - this.hzEff) : hz
    // jitter p95 of |Δw − 1/hz_eff|
    const m = this.nDw
    if (m === 0) return
    const ideal = 1000 / this.hzEff
    const t = this.tmp
    for (let i = 0; i < m; i++) {
      const v = Math.abs(this.dw[i] - ideal)
      let j = i - 1
      while (j >= 0 && t[j] > v) {
        t[j + 1] = t[j]
        j--
      }
      t[j + 1] = v
    }
    this.jitterMs = t[Math.min(m - 1, Math.floor(0.95 * m))]
  }
  /**
   * arrival statistics measured elsewhere (the rt.worker's 1 s window of the 60 Hz channels, header selHz and selJitterMs):
   * the main thread receives the latest sample per channel once per frame, so its own arrival times are frame times
   */
  setExternal(hz: number, jitterMs: number): void {
    this.hzEff = hz
    this.jitterMs = jitterMs
    this.last = Number.NaN
  }
  /** a pause: the next arrival must not create an interval spanning it */
  gap(): void {
    this.last = Number.NaN
  }
  reset(): void {
    this.hzEff = 0
    this.jitterMs = 0
    this.nArr = 0
    this.headArr = 0
    this.nDw = 0
    this.headDw = 0
    this.last = Number.NaN
  }
}

/** smoothed wall-clock delay of one channel (ms) */
export class WallDelay {
  value: number = P.dMaxMs
  held: number = P.dMaxMs
  private init = false
  target(hz: number, jitterMs: number): number {
    return hz > 0 ? Math.min(P.dMaxMs, Math.max(P.dMinMs, 2000 / hz + jitterMs)) : P.dMaxMs
  }
  tick(dtMs: number, hz: number, jitterMs: number): void {
    if (!(hz > 0)) return
    const tgt = this.target(hz, jitterMs)
    if (!this.init) {
      this.init = true
      this.value = tgt
      this.held = tgt
      return
    }
    if (Math.abs(tgt - this.held) > P.dHyst * this.held) this.held = tgt
    const step = (this.held - this.value) * (1 - Math.exp(-dtMs / P.dTauMs))
    const lim = P.dRatePerS * this.value * (dtMs / 1000)
    this.value += Math.max(-lim, Math.min(lim, step))
  }
}

/** the D_sim state machine of one channel: freeze decay, resume or epoch ramp, rate transition */
export class SimDelay {
  dSimMs = 0
  private frozenAt = Number.NaN
  private frozenFrom = 0
  private rampAt = Number.NaN
  private rampFrom = 0
  private needRamp = true
  private lastRate = Number.NaN

  tick(nowMs: number, advancing: boolean, rate: number, targetMs: number, reduced: boolean, zeroWhenFrozen: boolean): void {
    if (!advancing) {
      if (Number.isNaN(this.frozenAt)) {
        this.frozenAt = nowMs
        this.frozenFrom = this.dSimMs
      }
      this.needRamp = true
      this.rampAt = Number.NaN
      if (!zeroWhenFrozen) return
      if (reduced) {
        this.dSimMs = 0
        return
      }
      const u = Math.min(1, (nowMs - this.frozenAt) / MOTION.durationQuickMs)
      this.dSimMs = u >= 1 ? 0 : this.frozenFrom * (1 - bezierAt(EASE.smoothOut, u))
      return
    }
    this.frozenAt = Number.NaN
    if (this.needRamp) {
      this.needRamp = false
      this.rampAt = nowMs
      this.rampFrom = this.dSimMs
      this.lastRate = rate
    } else if (Math.abs(rate - this.lastRate) > 0.01 * Math.max(Math.abs(this.lastRate), 1e-3)) {
      this.rampAt = nowMs
      this.rampFrom = this.dSimMs
      this.lastRate = rate
    }
    if (!Number.isNaN(this.rampAt)) {
      const u = (nowMs - this.rampAt) / P.rampMs
      if (u >= 1) {
        this.rampAt = Number.NaN
        this.dSimMs = targetMs
      } else this.dSimMs = this.rampFrom + (targetMs - this.rampFrom) * Math.max(0, u)
    } else this.dSimMs = targetMs
  }
  /** new epoch: start from D = 0 and ramp on the next advancing frame */
  onEpoch(): void {
    this.dSimMs = 0
    this.frozenFrom = 0
    this.needRamp = true
    this.rampAt = Number.NaN
  }
  get ramping(): boolean {
    return !Number.isNaN(this.rampAt)
  }
}

export class DelayController {
  readonly swarm = new ArrivalStats()
  readonly focusStats = new ArrivalStats()
  readonly wall = new WallDelay()
  readonly focusWall = new WallDelay()
  readonly global = new SimDelay()
  readonly focusSim = new SimDelay()
  /** D_sim of the global render time and of the focus vehicle (ms of simulation time) */
  dSimMs = 0
  dFocusSimMs = 0
  focusActive = false
  /** 0 = global only, 1 = focus channel only (blended over 300 ms) */
  focusBlend = 0
  /** RK-M12-05 switch: frozen states decay D to 0 (default on) */
  zeroWhenFrozen = true
  private frozen = false

  get dWallMs(): number {
    return this.wall.value
  }
  get hzEff(): number {
    return this.swarm.hzEff
  }
  get jitterMs(): number {
    return this.swarm.jitterMs
  }
  get focusLowLatency(): boolean {
    return this.focusActive
  }

  /** a new swarm sample arrived at recvMs (main-thread time base); ignored while frozen */
  onSample(recvMs: number): void {
    if (this.frozen) {
      this.swarm.gap()
      return
    }
    this.swarm.onSample(recvMs)
  }
  /**
   * a new sample of the focus vehicle's 60 Hz channel ingested at recvMs; workerHz and workerJitterMs are the rt.worker's
   * arrival statistics of the 60 Hz channels (FrameHeader selHz, selJitterMs) and replace the main-thread arrival times
   * when known (FX2-R3, ADR-046: D_focus follows the 60 Hz channel, not the frame rate of the main thread)
   */
  onFocusSample(recvMs: number, workerHz = 0, workerJitterMs = Number.NaN): void {
    if (this.frozen) {
      this.focusStats.gap()
      return
    }
    if (workerHz > 0 && Number.isFinite(workerJitterMs)) this.focusStats.setExternal(workerHz, workerJitterMs)
    else this.focusStats.onSample(recvMs)
  }
  setFocus(active: boolean): void {
    if (active === this.focusActive) return
    this.focusActive = active
    if (active) this.focusStats.reset()
  }
  onEpoch(): void {
    this.global.onEpoch()
    this.focusSim.onEpoch()
    this.dSimMs = 0
    this.dFocusSimMs = 0
  }

  tick(nowMs: number, dtMs: number, advancing: boolean, rate: number, reduced = false): void {
    this.frozen = !advancing
    this.wall.tick(dtMs, this.swarm.hzEff, this.swarm.jitterMs)
    this.global.tick(nowMs, advancing, rate, rate * this.wall.value, reduced, this.zeroWhenFrozen)
    this.dSimMs = this.global.dSimMs
    // focus channel: its own wall delay when its samples arrive, else the global one
    const fh = this.focusStats.hzEff
    this.focusWall.tick(dtMs, fh, this.focusStats.jitterMs)
    const fWall = fh > 0 ? this.focusWall.value : this.wall.value
    this.focusSim.tick(nowMs, advancing, rate, rate * fWall, reduced, this.zeroWhenFrozen)
    const step = reduced ? 1 : dtMs / P.focusBlendMs
    this.focusBlend = this.focusActive ? Math.min(1, this.focusBlend + step) : Math.max(0, this.focusBlend - step)
    this.dFocusSimMs = (1 - this.focusBlend) * this.dSimMs + this.focusBlend * this.focusSim.dSimMs
  }
}
