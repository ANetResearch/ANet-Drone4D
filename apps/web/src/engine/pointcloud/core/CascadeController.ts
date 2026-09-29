// CAS cascade controller (ADR-012, ADR-041, ADR-044; M05 §6.8, M05-FR-038..046; product version of g02 ctrl.mjs
// CascadeController). Owner: M05. Pure: time, frame intervals and freeze masks are passed in; zero allocation per call.
//
// Inner loop: every presented interval is pushed into a 24-frame window; every 250 ms (>= 6 samples) the budget B is
// adjusted in the log domain from r = p50 / T*: r > 1.10 -> B x clamp((1/r)^0.8, 0.5, 0.92); else p90 > tailK T* ->
// B x 0.9; else two evaluations with r < 0.85 and no backlog -> B x 1.08; else (on target) every 8th evaluation B x 1.03,
// also with a backlog (ADR-012; the prototype refused it). B stays inside the effective band (quality floor, capacity
// clamp). Outer loop: a rung move only when the inner loop saturates - down one rung (B := new hi) after 1 s at lo with
// r > 1.2 unless the quality floor holds it; up one rung (B := new lo) after 3 s / 5 s at hi with headroom evidence and
// past upDelay, never into a degenerate rung. upDelay starts at 5 s and doubles when a down move follows an up move
// within 10 s (cap 120 s). Differences to the prototype: freezes, +3 % probing with a backlog, quality floor, capacity
// clamp and degenerate-rung guard, start B, saturation durations for PerfGovernor, manual lock.
import { LADDER, PC, isDegenerateRung, nonDegenerateAtOrBelow, type Rung } from '../params'

export const FREEZE_EXTERNAL = 1
export const FREEZE_SHADER_COMPILE = 2
export const FREEZE_WARMUP = 4

export type RungReason = 'overload' | 'headroom' | 'manual'

export interface CasOptions {
  ladder?: readonly Rung[]
  /** be.startRung; falls back to the highest non-degenerate rung at or below it */
  startIndex: number
  /** lowest rung the automatic mode may use before PerfGovernor step 7 */
  floorIndex: number
  /** automatic ceiling (software 1, iGPU 4, dGPU 5), capped at the highest non-degenerate rung */
  ceilIndex: number
  targetMs: number
  tailK: number
  /** start B (software 25k); default the start rung's clamped hi */
  initialB?: number
  /** B_floor before step 7 (software 20k, hardware the floor rung's lo) */
  Bfloor: number
  poolCapacityPts: number
  /** render scale lock (Tier S 0.5); 0 follows the rung */
  rsLock?: number
  onRung?: (from: number, to: number, reason: RungReason) => void
}

export interface CasState {
  atFloorSinceMs: number
  atFloorTotalMs: number
  atCeilSinceMs: number
  B: number
  lo: number
  hi: number
  rung: number
  rs: number
  Bfloor: number
}

class Ring {
  readonly buf: Float64Array
  readonly tmp: Float64Array
  size = 0
  private head = 0
  constructor(cap: number) {
    this.buf = new Float64Array(cap)
    this.tmp = new Float64Array(cap)
  }
  push(v: number): void {
    this.buf[this.head] = v
    this.head = (this.head + 1) % this.buf.length
    if (this.size < this.buf.length) this.size++
  }
  clear(): void {
    this.size = 0
    this.head = 0
  }
  /** sorts the window into tmp (ascending); q(p) = tmp[floor(n p)] as in ctrl.mjs */
  sort(): void {
    const n = this.size
    const start = (this.head - n + this.buf.length) % this.buf.length
    for (let i = 0; i < n; i++) this.tmp[i] = this.buf[(start + i) % this.buf.length]
    for (let i = n; i < this.tmp.length; i++) this.tmp[i] = Number.POSITIVE_INFINITY
    this.tmp.sort()
  }
  q(p: number): number {
    return this.tmp[Math.min(this.size - 1, Math.floor(this.size * p))]
  }
}

export class CascadeController {
  readonly ladder: readonly Rung[]
  readonly poolCapacityPts: number
  index: number
  B: number
  manual = false
  override = false
  floorIndex: number
  readonly floorIndex0: number
  readonly ceilIndex: number
  T: number
  tailK: number
  Bfloor: number
  readonly Bfloor0: number
  readonly rsLock: number
  // telemetry (__perf.cas)
  evals = 0
  frozenFrames = 0
  rungChanges = 0
  bounces = 0
  reversals = 0
  /** last evaluated p50 / T* (inBandAtMs) */
  lastR = Number.NaN
  lastP50 = Number.NaN
  lastNow = 0
  onRung: ((from: number, to: number, reason: RungReason) => void) | null

  private readonly win = new Ring(PC.casWindow)
  private readonly ww = new Ring(PC.casWindow)
  private lastEval = Number.NEGATIVE_INFINITY
  private good = 0
  private onT = 0
  private loSince = -1
  private floorSince = -1
  private floorTotalMs = 0
  private hiSince = -1
  private ceilSince = -1
  private lastUp = Number.NEGATIVE_INFINITY
  private lastChange = Number.NEGATIVE_INFINITY
  private upDelay: number = PC.casUpDelayMs
  private lastDir = 0
  private lastRungDir = 0
  private lastRungAt = Number.NEGATIVE_INFINITY
  private readonly st: CasState = { atFloorSinceMs: 0, atFloorTotalMs: 0, atCeilSinceMs: 0, B: 0, lo: 0, hi: 0, rung: 0, rs: 1, Bfloor: 0 }
  private readonly bandOut = new Float64Array(2)

  constructor(o: CasOptions) {
    this.ladder = o.ladder ?? LADDER
    this.poolCapacityPts = o.poolCapacityPts
    this.floorIndex0 = Math.max(0, o.floorIndex)
    this.floorIndex = this.floorIndex0
    this.ceilIndex = nonDegenerateAtOrBelow(o.ceilIndex, o.poolCapacityPts, this.ladder)
    this.index = nonDegenerateAtOrBelow(o.startIndex, o.poolCapacityPts, this.ladder)
    this.T = o.targetMs
    this.tailK = o.tailK
    this.Bfloor0 = o.Bfloor
    this.Bfloor = o.Bfloor
    this.rsLock = o.rsLock ?? 0
    this.onRung = o.onRung ?? null
    this.band()
    const b0 = o.initialB ?? this.bandOut[1]
    this.B = Math.min(Math.max(b0, this.bandOut[0]), this.bandOut[1])
  }

  get rung(): Rung {
    return this.ladder[this.index]
  }
  get capClamp(): number {
    return PC.capacityFrac * this.poolCapacityPts
  }
  /** the band is clamped by the pool capacity (HUD badge, M05-FR-025) */
  get clampedByCapacity(): boolean {
    return this.rung.hi > this.capClamp
  }
  /** B is held by the quality floor at the band's lower edge */
  get floorHeld(): boolean {
    return this.B <= this.bandOut[0] * 1.001 && this.floorHolding()
  }

  /** effective band [lo, hi] of the current rung into bandOut (M05 §6.8.4) */
  band(): Float64Array {
    const r = this.rung
    const floorLo = this.override ? r.lo : Math.max(r.lo, this.Bfloor)
    const hi = Math.min(r.hi, this.capClamp)
    this.bandOut[0] = Math.min(floorLo, hi)
    this.bandOut[1] = hi
    return this.bandOut
  }
  get lo(): number {
    return this.band()[0]
  }
  get hi(): number {
    return this.band()[1]
  }

  /** the quality floor holds B: report saturation to PerfGovernor instead of moving down */
  floorHolding(): boolean {
    return !this.override && (this.Bfloor > this.rung.lo || this.index <= this.floorIndex)
  }

  isDegenerate(k: number): boolean {
    return isDegenerateRung(this.ladder[k], this.poolCapacityPts)
  }

  private resetSat(): void {
    this.loSince = -1
    this.floorSince = -1
    this.hiSince = -1
    this.ceilSince = -1
  }

  private countReversal(Bnew: number, Bold: number): void {
    if (!(Bold > 0)) return
    const rel = (Bnew - Bold) / Bold
    if (Math.abs(rel) <= PC.casReversalRel) return
    const d = rel > 0 ? 1 : -1
    if (this.lastDir !== 0 && d !== this.lastDir) this.reversals++
    this.lastDir = d
  }

  private moveTo(k: number, now: number, reason: RungReason, counted: boolean): void {
    const from = this.index
    if (k === from) return
    this.index = k
    this.win.clear()
    this.ww.clear()
    this.resetSat()
    this.good = 0
    this.onT = 0
    if (counted) {
      this.rungChanges++
      const dir = k > from ? 1 : -1
      if (this.lastRungDir !== 0 && dir !== this.lastRungDir && now - this.lastRungAt < PC.casBounceMs) this.bounces++
      this.lastRungDir = dir
      this.lastRungAt = now
    }
    this.lastChange = now
    this.onRung?.(from, k, reason)
  }

  /**
   * One presented frame. freezeMask != 0 (EXTERNAL | SHADER_COMPILE | WARMUP) skips evaluation and clears the windows.
   * pending: backlog (pending uploads > 2 frame quotas, or all fetch slots busy). Returns -1, 0 or +1 (rung move).
   */
  sample(dtMs: number, nowMs: number, freezeMask: number, pending: boolean, workMs?: number): -1 | 0 | 1 {
    this.lastNow = nowMs
    if (freezeMask !== 0) {
      this.win.clear()
      this.ww.clear()
      this.frozenFrames++
      this.resetSat()
      return 0
    }
    if (!(dtMs > 0) || dtMs > PC.casMaxDtMs) return 0
    this.win.push(dtMs)
    if (workMs !== undefined && Number.isFinite(workMs)) this.ww.push(workMs)
    if (nowMs - this.lastEval < PC.casEvalMs || this.win.size < PC.casMinSamples) return 0
    const dEval = Number.isFinite(this.lastEval) ? Math.min(nowMs - this.lastEval, PC.casMaxDtMs) : 0
    this.lastEval = nowMs
    this.evals++
    this.win.sort()
    const p50 = this.win.q(0.5)
    const p90 = this.win.q(0.9)
    const p95 = this.win.q(0.95)
    const T = this.T
    const r = p50 / T
    this.lastR = r
    this.lastP50 = p50
    const tail = p90 > this.tailK * T
    this.band()
    const lo = this.bandOut[0]
    const hi = this.bandOut[1]
    const B0 = this.B
    let B = B0
    if (r > PC.casOverR) {
      B *= Math.min(Math.max((1 / r) ** PC.casDownExp, PC.casDownMin), PC.casDownMax)
      this.good = 0
      this.onT = 0
    } else if (tail) {
      B *= PC.casTailDown
      this.good = 0
      this.onT = 0
    } else if (r < PC.casUnderR) {
      this.onT = 0
      if (!pending && ++this.good >= PC.casUpGood) B *= PC.casUp
    } else {
      this.good = 0
      if (++this.onT >= PC.casProbeEvals) {
        B *= PC.casProbe // ADR-012: the slow probe is allowed with a backlog
        this.onT = 0
      }
    }
    B = Math.min(Math.max(B, lo), hi)
    this.countReversal(B, B0)
    const atLo = B <= lo * 1.001
    const atHi = B >= hi * 0.999
    this.loSince = atLo && r > PC.casRungDownR ? (this.loSince < 0 ? nowMs : this.loSince) : -1
    this.floorSince = atLo && this.floorHolding() && (r > PC.casOverR || tail) ? (this.floorSince < 0 ? nowMs : this.floorSince) : -1
    if (this.floorSince >= 0) this.floorTotalMs += dEval
    this.ww.sort()
    const wm = this.ww.size >= PC.casMinSamples ? this.ww.q(0.5) : undefined
    const headroom = wm !== undefined ? wm < PC.casWorkHeadroom * T : r < PC.casHeadroomR || p95 <= PC.casHeadroomP95 * T
    this.hiSince = atHi && headroom ? (this.hiSince < 0 ? nowMs : this.hiSince) : -1
    this.ceilSince = atHi || B >= PC.casCeilFrac * hi ? (this.ceilSince < 0 ? nowMs : this.ceilSince) : -1
    if (!this.manual && this.loSince >= 0 && nowMs - this.loSince >= PC.casRungDownMs && this.index > this.floorIndex && !this.floorHolding()) {
      if (nowMs - this.lastUp < PC.casBounceMs) this.upDelay = Math.min(this.upDelay * 2, PC.casUpDelayMaxMs)
      this.moveTo(this.index - 1, nowMs, 'overload', true)
      this.B = this.band()[1]
      this.countReversal(this.B, B)
      return -1
    }
    const needMs = r < PC.casHeadroomR || wm !== undefined ? PC.casUpFastMs : PC.casUpSlowMs
    if (!this.manual && this.hiSince >= 0 && nowMs - this.hiSince >= needMs && this.index < this.ceilIndex && !this.isDegenerate(this.index + 1)
      && nowMs - this.lastChange > this.upDelay) {
      this.moveTo(this.index + 1, nowMs, 'headroom', true)
      this.lastUp = nowMs
      this.B = this.band()[0]
      this.countReversal(this.B, B)
      return 1
    }
    this.B = B
    return 0
  }

  /** manual rung lock (M05-FR-044): the outer loop stops, the inner loop keeps B inside the rung; null unlocks */
  setManual(index: number | null): void {
    if (index === null) {
      this.manual = false
      return
    }
    const k = Math.min(Math.max(0, Math.round(index)), this.ladder.length - 1)
    this.manual = true
    if (k !== this.index) this.moveTo(k, this.lastNow, 'manual', false)
    const b = this.band()
    this.B = Math.min(Math.max(this.B, b[0]), b[1])
  }

  /** PerfGovernor step 7 (ADR-041): release the quality floor (software B_floor = lo, hardware floorIndex = 0); null restores */
  setFloorOverride(lo: number | null): void {
    if (lo === null) {
      this.override = false
      this.Bfloor = this.Bfloor0
      this.floorIndex = this.floorIndex0
    } else {
      this.override = true
      this.Bfloor = lo
      this.floorIndex = 0
    }
    const b = this.band()
    this.B = Math.min(Math.max(this.B, b[0]), b[1])
  }

  /** hardware T* from M06's refresh estimate (changes > 5 %) */
  setTarget(targetMs: number, tailK: number): void {
    if (targetMs > 0) this.T = targetMs
    if (tailK > 0) this.tailK = tailK
  }

  /** force B (test builds: fixedB); stays inside nothing, the controller is bypassed by the engine */
  setB(B: number): void {
    this.B = B
  }

  /** preallocated state for PerfGovernor (M05 §6.8.5): durations in ms, zero while frozen or not saturated */
  state(nowMs: number = this.lastNow): Readonly<CasState> {
    const s = this.st
    const b = this.band()
    s.atFloorSinceMs = this.floorSince < 0 ? 0 : nowMs - this.floorSince
    s.atFloorTotalMs = this.floorTotalMs
    s.atCeilSinceMs = this.ceilSince < 0 ? 0 : nowMs - this.ceilSince
    s.B = this.B
    s.lo = b[0]
    s.hi = b[1]
    s.rung = this.index
    s.rs = this.rsLock > 0 ? this.rsLock : this.rung.rs
    s.Bfloor = this.Bfloor
    return s
  }
}
