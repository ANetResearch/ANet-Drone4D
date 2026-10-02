// InterpRing (M12 §6.4, FR-009 to FR-012; ADR-046; r14 §3.6, r15 §3.9, r18 §3.3). Owner: M12.
// K = 32 samples per vehicle in preallocated SoA rings: t (f64 ms from the run start), position (World ENU m), velocity
// (m/s), body angular rate ω (rad/s, NaN for Lite32 samples), attitude [x, y, z, w] (WORLD <- BODY FLU) and the source
// (0 Lite32, 1 Full64). Capacity grows in steps of 256 vehicles (only when an unseen agent appears, i.e. a roster change).
// Writes: newer samples append; a sample at the newest time replaces it when its source ranks >= (Full64 over Lite32);
// out-of-order samples between the oldest and the newest are inserted in time order (at most K - 1 moves); samples older
// than the oldest are dropped. Sampling at t: bracket a.t <= t < b.t gives the cubic Hermite position (sample velocities
// as tangents), linear velocity and slerp attitude; beyond the newest sample the position extrapolates with the velocity
// and the attitude integrates ω (Full64) for at most E_max = rate × 3 / hz_eff (simulation ms), after which the vehicle
// is HOLD (frozen at E_max); before the oldest sample it clamps to it. ingest and sampling allocate nothing.
import { DS64 } from '@awr/contracts/layouts'
import type { TelemetryFrame } from '@/net/rt/types'
import { hermiteRing, integrateOmegaInto, slerpInto } from './hermite'
import { TIME_PARAMS as P } from './params'
import { hypot4 } from '../hypot'

export { slerpInto } from './hermite'

/** output of sampleSwarm, preallocated by the caller (M06) for the roster capacity */
export interface DronePoseSoA {
  n: number
  agentNo: Uint16Array
  pos: Float32Array
  quat: Float32Array
  vel: Float32Array
  state: Uint8Array
  flags: Uint8Array
  battery: Uint8Array
  hold: Uint8Array
  clamped: Uint8Array
  /** ms: the newest sample used (the bracket's later sample, or the extrapolation base) */
  sampleT: Float64Array
  /** wall-clock seconds since the sample the pose rests on ("signal delay") */
  ageS: Float32Array
}

export function newDronePoseSoA(cap: number): DronePoseSoA {
  return {
    n: 0, agentNo: new Uint16Array(cap), pos: new Float32Array(3 * cap), quat: new Float32Array(4 * cap), vel: new Float32Array(3 * cap),
    state: new Uint8Array(cap), flags: new Uint8Array(cap), battery: new Uint8Array(cap), hold: new Uint8Array(cap), clamped: new Uint8Array(cap),
    sampleT: new Float64Array(cap), ageS: new Float32Array(cap),
  }
}

/** the interpolation interface consumed by M06 (M12 §7.1) */
export interface Interp {
  sampleSwarm(tS: number, out: DronePoseSoA): void
  sampleOne(agentNo: number, tS: number, out: DronePoseSoA, o?: number): boolean
  setFocus(agentNo: number): void
}

const FULL_ITEM = 80
/** control-byte bits that a command changes: owner (0-2) and native mode (4-5) */
const CTRL_EFFECT = 0x37
const FULL_ROW = 16

export class InterpRing implements Interp {
  readonly K: number
  cap = 0
  /** slots handed out (slot i belongs to agentOfSlot[i]) */
  n = 0
  t = new Float64Array(0)
  p = new Float32Array(0)
  v = new Float32Array(0)
  w = new Float32Array(0)
  q = new Float32Array(0)
  src = new Uint8Array(0)
  head = new Int32Array(0)
  count = new Int32Array(0)
  state = new Uint8Array(0)
  /** control byte (owner | locked << 3 | native << 4 | pose_src << 6) of the newest sample */
  ctrl = new Uint8Array(0)
  /**
   * simulation ms of the first sample that carried the current flight state byte and control owner/native (a command's
   * visible effect, D1-AC-26 command to visible)
   */
  stateSince = new Float64Array(0)
  flags = new Uint8Array(0)
  battery = new Uint8Array(0)
  agentOfSlot = new Uint16Array(0)
  readonly slotOf = new Int32Array(65536).fill(-1)
  /** extrapolation limit, simulation ms (written every advancing frame by the clock phase) */
  eMaxMs = 300
  /** nominal rate (last advancing TIME.rate) for the wall-clock age */
  rate = 1
  /** vehicle of the focus exception (-1 none); the clock reads it for D_focus */
  focusAgent = -1
  /** statistics of the last sampleSwarm: vehicles, HOLD, extrapolating, max age (wall ms) */
  lastN = 0
  lastHold = 0
  lastExtrap = 0
  lastMaxAgeMs = 0
  /** tSampleMs of the focus vehicle's newest Full64 (new-sample detection for the focus channel) */
  focusSampleMs = Number.NaN
  onFocusChange: ((agentNo: number) => void) | null = null

  constructor(capacity: number = P.ringStep, K: number = P.ringK) {
    this.K = K
    this.grow(capacity)
  }

  /** capacity in vehicles; call on roster changes only */
  ensureCapacity(n: number): void {
    if (n > this.cap) this.grow(n)
  }

  private grow(cap: number): void {
    const c = Math.ceil(Math.max(1, cap) / P.ringStep) * P.ringStep
    if (c <= this.cap) return
    const K = this.K
    const cp = <T extends Float64Array | Float32Array | Uint8Array | Int32Array | Uint16Array>(a: T, n: number): T => {
      const b = new (a.constructor as new (n: number) => T)(n)
      b.set(a)
      return b
    }
    this.t = cp(this.t, c * K)
    this.p = cp(this.p, 3 * c * K)
    this.v = cp(this.v, 3 * c * K)
    this.w = cp(this.w, 3 * c * K)
    this.q = cp(this.q, 4 * c * K)
    this.src = cp(this.src, c * K)
    this.head = cp(this.head, c)
    this.count = cp(this.count, c)
    this.state = cp(this.state, c)
    this.stateSince = cp(this.stateSince, c)
    this.ctrl = cp(this.ctrl, c)
    this.flags = cp(this.flags, c)
    this.battery = cp(this.battery, c)
    this.agentOfSlot = cp(this.agentOfSlot, c)
    this.cap = c
  }

  /** onEpoch: every ring empties (M12 §6.4) */
  clearAll(): void {
    this.count.fill(0)
    this.focusSampleMs = Number.NaN
  }

  /** onReset: only the slots of agents in [idBase, idBase + idCount) (one producer's id range) */
  clearRange(idBase: number, idCount: number): void {
    const hi = idBase + idCount
    for (let s = 0; s < this.n; s++) {
      const a = this.agentOfSlot[s]
      if (a >= idBase && a < hi) this.count[s] = 0
    }
  }

  slotFor(agentNo: number): number {
    let s = this.slotOf[agentNo]
    if (s >= 0) return s
    if (this.n >= this.cap) this.grow(this.n + 1)
    s = this.n++
    this.slotOf[agentNo] = s
    this.agentOfSlot[s] = agentNo
    this.count[s] = 0
    this.head[s] = 0
    return s
  }

  setExtrapLimitS(eMaxSimS: number): void {
    this.eMaxMs = eMaxSimS * 1000
  }

  setFocus(agentNo: number): void {
    const a = agentNo >= 0 && agentNo < 65536 ? agentNo : -1
    if (a === this.focusAgent) return
    this.focusAgent = a
    this.focusSampleMs = Number.NaN
    this.onFocusChange?.(a)
  }

  private write(i: number, tMs: number, px: number, py: number, pz: number, vx: number, vy: number, vz: number,
    qx: number, qy: number, qz: number, qw: number, src: number, wx: number, wy: number, wz: number): void {
    const ql = hypot4(qx, qy, qz, qw) || 1
    this.t[i] = tMs
    const i3 = 3 * i
    const i4 = 4 * i
    this.p[i3] = px
    this.p[i3 + 1] = py
    this.p[i3 + 2] = pz
    this.v[i3] = vx
    this.v[i3 + 1] = vy
    this.v[i3 + 2] = vz
    this.w[i3] = wx
    this.w[i3 + 1] = wy
    this.w[i3 + 2] = wz
    this.q[i4] = qx / ql
    this.q[i4 + 1] = qy / ql
    this.q[i4 + 2] = qz / ql
    this.q[i4 + 3] = qw / ql
    this.src[i] = src
  }

  private move(from: number, to: number): void {
    this.t[to] = this.t[from]
    for (let j = 0; j < 3; j++) {
      this.p[3 * to + j] = this.p[3 * from + j]
      this.v[3 * to + j] = this.v[3 * from + j]
      this.w[3 * to + j] = this.w[3 * from + j]
    }
    for (let j = 0; j < 4; j++) this.q[4 * to + j] = this.q[4 * from + j]
    this.src[to] = this.src[from]
  }

  /** push one sample (tMs simulation time); the quaternion is normalised here; src 0 Lite32, 1 Full64; ω NaN when absent */
  push(s: number, tMs: number, px: number, py: number, pz: number, vx: number, vy: number, vz: number,
    qx: number, qy: number, qz: number, qw: number, src: number, wx = Number.NaN, wy = Number.NaN, wz = Number.NaN): void {
    const K = this.K
    const base = s * K
    const cnt = this.count[s]
    let h = this.head[s]
    if (cnt === 0) {
      this.head[s] = 0
      this.count[s] = 1
      this.write(base, tMs, px, py, pz, vx, vy, vz, qx, qy, qz, qw, src, wx, wy, wz)
      return
    }
    const ni = base + h
    const newest = this.t[ni]
    if (tMs > newest) {
      h = (h + 1) % K
      this.head[s] = h
      this.count[s] = Math.min(cnt + 1, K)
      this.write(base + h, tMs, px, py, pz, vx, vy, vz, qx, qy, qz, qw, src, wx, wy, wz)
      return
    }
    if (tMs === newest) {
      if (src >= this.src[ni]) this.write(ni, tMs, px, py, pz, vx, vy, vz, qx, qy, qz, qw, src, wx, wy, wz)
      return
    }
    // out of order: k = number of samples newer than tMs
    let k = 0
    let idx = ni
    for (; k < cnt; k++) {
      idx = base + ((h - k + K) % K)
      if (this.t[idx] <= tMs) break
    }
    if (k === cnt) return // older than the oldest sample: dropped
    if (this.t[idx] === tMs) {
      if (src >= this.src[idx]) this.write(idx, tMs, px, py, pz, vx, vy, vz, qx, qy, qz, qw, src, wx, wy, wz)
      return
    }
    if (cnt < K) {
      for (let j = 0; j < k; j++) this.move(base + ((h - j + K) % K), base + ((h - j + 1 + K) % K))
      h = (h + 1) % K
      this.head[s] = h
      this.count[s] = cnt + 1
      this.write(base + ((h - k + K) % K), tMs, px, py, pz, vx, vy, vz, qx, qy, qz, qw, src, wx, wy, wz)
    } else {
      // full: the oldest sample drops, the older part shifts down one position
      const L = cnt - 1 - k
      const o0 = (h - cnt + 1 + 2 * K) % K
      for (let j = 0; j < L; j++) this.move(base + ((o0 + j + 1) % K), base + ((o0 + j) % K))
      this.write(base + ((o0 + L) % K), tMs, px, py, pz, vx, vy, vz, qx, qy, qz, qw, src, wx, wy, wz)
    }
  }

  /**
   * Swarm rows (Lite32, slot SoA already scaled: vel m/s, quat unit) and Full64 items of one TelemetryFrame; the latest
   * state bytes follow the sample. Returns true when a new sample of the focus vehicle arrived.
   */
  ingest(frame: TelemetryFrame): boolean {
    const h = frame.hdr
    let focusNew = false
    if (h.swarmN > 0) {
      const sw = frame.swarm
      const t = h.swarmTSimMs
      const n = h.swarmN
      if (this.n + n > this.cap) this.ensureCapacity(this.n + n)
      for (let i = 0; i < n; i++) {
        const s = this.slotFor(sw.agentNo[i])
        const i3 = 3 * i
        const i4 = 4 * i
        this.push(s, t, sw.pos[i3], sw.pos[i3 + 1], sw.pos[i3 + 2], sw.vel[i3], sw.vel[i3 + 1], sw.vel[i3 + 2],
          sw.quat[i4], sw.quat[i4 + 1], sw.quat[i4 + 2], sw.quat[i4 + 3], 0)
        const ck = sw.ctrl[i] & CTRL_EFFECT
        if (this.state[s] !== sw.fs[i] || (this.ctrl[s] & CTRL_EFFECT) !== ck || this.count[s] === 1) this.stateSince[s] = t
        this.state[s] = sw.fs[i]
        this.ctrl[s] = sw.ctrl[i]
        this.flags[s] = sw.flags[i]
        this.battery[s] = sw.battery[i]
      }
    }
    const fr = frame.full
    const dv = fr.bytes
    for (let k = 0; k < fr.count; k++) {
      const o = fr.base + FULL_ITEM * k
      const agent = dv.getUint16(o, true)
      const tMs = dv.getFloat64(o + 8, true)
      const r = o + FULL_ROW
      const s = this.slotFor(agent)
      this.push(s, tMs,
        dv.getFloat32(r + DS64.POS, true), dv.getFloat32(r + DS64.POS + 4, true), dv.getFloat32(r + DS64.POS + 8, true),
        dv.getFloat32(r + DS64.VEL, true), dv.getFloat32(r + DS64.VEL + 4, true), dv.getFloat32(r + DS64.VEL + 8, true),
        dv.getFloat32(r + DS64.Q, true), dv.getFloat32(r + DS64.Q + 4, true), dv.getFloat32(r + DS64.Q + 8, true), dv.getFloat32(r + DS64.Q + 12, true), 1,
        dv.getFloat32(r + DS64.OMEGA, true), dv.getFloat32(r + DS64.OMEGA + 4, true), dv.getFloat32(r + DS64.OMEGA + 8, true))
      if (tMs >= this.t[s * this.K + this.head[s]]) {
        const fs = dv.getUint8(r + DS64.FLIGHT_STATE)
        const cb = dv.getUint8(r + DS64.CTRL)
        if (this.state[s] !== fs || (this.ctrl[s] & CTRL_EFFECT) !== (cb & CTRL_EFFECT) || this.count[s] === 1) this.stateSince[s] = tMs
        this.state[s] = fs
        this.ctrl[s] = cb
        this.flags[s] = dv.getUint8(r + DS64.FLAGS)
        this.battery[s] = dv.getUint8(r + DS64.BATTERY_PCT)
      }
      if (agent === this.focusAgent && !(tMs <= this.focusSampleMs)) {
        this.focusSampleMs = tMs
        focusNew = true
      }
    }
    return focusNew
  }

  /** sample every vehicle with samples at tS (s); out.n = vehicles written */
  sampleSwarm(tS: number, out: DronePoseSoA): void {
    const t = tS * 1000
    const cap = out.agentNo.length
    let n = 0
    let hold = 0
    let extrap = 0
    let maxAge = 0
    for (let s = 0; s < this.n && n < cap; s++) {
      if (this.count[s] === 0) continue
      const r = this.sampleSlot(s, t, out, n)
      if (r > 0) {
        extrap++
        if (out.hold[n] === 1) hold++
      }
      const age = out.ageS[n] * 1000
      if (age > maxAge) maxAge = age
      n++
    }
    out.n = n
    this.lastN = n
    this.lastHold = hold
    this.lastExtrap = extrap
    this.lastMaxAgeMs = maxAge
  }

  /** simulation ms since which the vehicle's newest flight state and control owner/native hold (NaN without samples) */
  stateSinceMs(agentNo: number): number {
    const s = this.slotOf[agentNo]
    return s < 0 || this.count[s] === 0 ? Number.NaN : this.stateSince[s]
  }
  /** control byte of the vehicle's newest sample (-1 without samples) */
  ctrlOf(agentNo: number): number {
    const s = this.slotOf[agentNo]
    return s < 0 || this.count[s] === 0 ? -1 : this.ctrl[s]
  }

  /** one vehicle into out at index o; false when it has no samples */
  sampleOne(agentNo: number, tS: number, out: DronePoseSoA, o = 0): boolean {
    const s = this.slotOf[agentNo]
    if (s < 0 || this.count[s] === 0) return false
    this.sampleSlot(s, tS * 1000, out, o)
    return true
  }

  /** returns 1 when extrapolating (t beyond the newest sample), -1 when clamped, 0 when interpolated */
  private sampleSlot(s: number, t: number, out: DronePoseSoA, o: number): number {
    const K = this.K
    const base = s * K
    const cnt = this.count[s]
    const h = this.head[s]
    out.agentNo[o] = this.agentOfSlot[s]
    out.state[o] = this.state[s]
    out.flags[o] = this.flags[s]
    out.battery[o] = this.battery[s]
    out.hold[o] = 0
    out.clamped[o] = 0
    const rate = Math.max(this.rate, 1e-3)
    const ni = base + h
    const tn = this.t[ni]
    const o3 = 3 * o
    if (t >= tn) {
      const age = t - tn
      const dt = Math.min(age, Math.max(0, this.eMaxMs))
      const f = dt / 1000
      const n3 = 3 * ni
      out.pos[o3] = this.p[n3] + this.v[n3] * f
      out.pos[o3 + 1] = this.p[n3 + 1] + this.v[n3 + 1] * f
      out.pos[o3 + 2] = this.p[n3 + 2] + this.v[n3 + 2] * f
      out.vel[o3] = this.v[n3]
      out.vel[o3 + 1] = this.v[n3 + 1]
      out.vel[o3 + 2] = this.v[n3 + 2]
      if (f > 0 && this.w[n3] === this.w[n3]) integrateOmegaInto(this.q, 4 * ni, this.w, n3, f, out.quat, 4 * o)
      else for (let j = 0; j < 4; j++) out.quat[4 * o + j] = this.q[4 * ni + j]
      out.hold[o] = age > this.eMaxMs ? 1 : 0
      out.sampleT[o] = tn
      out.ageS[o] = age / 1000 / rate
      return age > 0 ? 1 : 0
    }
    // walk back to the bracket a.t <= t < b.t
    let bi = ni
    let ai = -1
    for (let k = 1; k < cnt; k++) {
      const idx = base + ((h - k + K) % K)
      if (this.t[idx] <= t) {
        ai = idx
        break
      }
      bi = idx
    }
    if (ai < 0) {
      const b3 = 3 * bi
      for (let j = 0; j < 3; j++) {
        out.pos[o3 + j] = this.p[b3 + j]
        out.vel[o3 + j] = this.v[b3 + j]
      }
      for (let j = 0; j < 4; j++) out.quat[4 * o + j] = this.q[4 * bi + j]
      out.clamped[o] = 1
      out.sampleT[o] = this.t[bi]
      out.ageS[o] = 0
      return -1
    }
    const ta = this.t[ai]
    const tb = this.t[bi]
    const hS = (tb - ta) / 1000
    const u = (t - ta) / (tb - ta)
    hermiteRing(this.p, this.v, ai, bi, hS, u, out.pos, o3)
    const a3 = 3 * ai
    const b3 = 3 * bi
    out.vel[o3] = (1 - u) * this.v[a3] + u * this.v[b3]
    out.vel[o3 + 1] = (1 - u) * this.v[a3 + 1] + u * this.v[b3 + 1]
    out.vel[o3 + 2] = (1 - u) * this.v[a3 + 2] + u * this.v[b3 + 2]
    slerpInto(this.q, 4 * ai, this.q, 4 * bi, u, out.quat, 4 * o)
    out.sampleT[o] = tb
    out.ageS[o] = (t - ta) / 1000 / rate
    return 0
  }

  /** samples of one slot in time order (tests and diagnostics; allocates) */
  timesOf(agentNo: number): number[] {
    const s = this.slotOf[agentNo]
    if (s < 0) return []
    const K = this.K
    const cnt = this.count[s]
    const h = this.head[s]
    const out: number[] = []
    for (let k = cnt - 1; k >= 0; k--) out.push(this.t[s * K + ((h - k + K) % K)])
    return out
  }
}
