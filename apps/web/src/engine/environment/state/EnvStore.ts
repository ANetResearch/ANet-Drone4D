// EnvStore: client evaluation of the server-authoritative environment (M07-FR-030-FR-032; M07 §6.3.10, §6.3.15,
// §6.6.2; ADR-046). Owner: M07.
// ingest() (rt data, a few Hz, may allocate) decodes a keyframe and queues it by version (<= 4); update() (loop 'world'
// phase, zero allocation) makes current the highest version with t_apply_ns <= tRender, integrates the anchors on the
// 20 ms grid up to tRender (same operations as the server), evaluates eval_env and derive at tRender and refreshes the
// damped visual scalars. States: EMPTY -> SYNCED (C01) -> STALE after 3 s wall without frames (C04) -> SYNCED on any
// frame (C05); a global or producer epoch change freezes visuals and waits for a frame of the new epoch (C06, C07);
// a presets hash mismatch asks the adapter to fetch GET /api/env/presets and evaluates with the server model (C08).
import { advance, backstep, H_NS, partial } from './anchors'
import { AnchorSmoother, VisualDamper } from './damping'
import { derive, newDerived, type EnvDerived } from './derive'
import { evalEnv } from './evalEnv'
import { copyAnchors, decodeKeyframe, MODE_STEP, newAnchors, type Anchors, type EnvKeyframe, type EnvKeyframeWire } from './keyframe'
import { NF, PRESETS_MODEL, type PresetsModel } from './presets'

export type EnvSyncState = 'EMPTY' | 'SYNCED' | 'STALE' | 'EPOCH_WAIT'
export const QUEUE_MAX = 4
const RING = 64
/** grid steps integrated per frame at most (20x replay at 30 fps needs 34; larger gaps catch up over frames) */
export const MAX_STEPS_PER_FRAME = 4096
/** first-frame back-integration limit (15 x rate grid steps at most, M07 §6.3.10) */
const MAX_BACK_STEPS = 3000

export interface EnvStoreOptions {
  staleWallMs?: number
  snapM?: number
  snapWetness?: number
}

export class EnvStore {
  state: EnvSyncState = 'EMPTY'
  P: PresetsModel = PRESETS_MODEL
  /** hash of the frame that did not match the loaded model (adapter fetches /api/env/presets), else null */
  presetsWanted: string | null = null
  presetsMismatch = false
  /** current keyframe (t_apply_ns <= tRender) */
  current: EnvKeyframe | null = null
  readonly queue: EnvKeyframe[] = []
  /** 21 scalars at tRender (physics truth, not damped) */
  readonly scalars = new Float64Array(NF)
  readonly derived: EnvDerived = newDerived()
  /** anchors at tRender (integrated + partial; smoothing offsets added for visuals only in `anchorsVis`) */
  readonly anchors: Anchors = newAnchors()
  readonly anchorsVis: Anchors = newAnchors()
  readonly damper = new VisualDamper()
  readonly smoother = new AnchorSmoother()
  /** derived quantities of the damped visual scalars */
  readonly derivedVis: EnvDerived = newDerived()
  tRenderNs = 0
  version = 0
  /** step switch counter (tests, perf) */
  switches = 0
  /** anchor drift of the last heartbeat check (m), for __perf.env */
  anchorDriftM = 0
  lastRecvMs = Number.NEGATIVE_INFINITY
  globalEpoch = -1
  private readonly A: Anchors = newAnchors() // grid-aligned anchors of the current keyframe
  private kA = 0
  private readonly ring: Anchors[] = Array.from({ length: RING }, () => newAnchors())
  private ringHead = -1
  private readonly tmpA: Anchors = newAnchors()
  private readonly err = new Float64Array(8)
  private readonly opts: Required<EnvStoreOptions>

  constructor(o: EnvStoreOptions = {}) {
    const c = PRESETS_MODEL.client as Record<string, number>
    this.opts = {
      staleWallMs: o.staleWallMs ?? (c.stale_wall_s ?? 3) * 1000,
      snapM: o.snapM ?? c.anchor_snap_m ?? 1,
      snapWetness: o.snapWetness ?? c.anchor_snap_wetness ?? 0.02,
    }
  }

  /** C09: world switch or disconnect */
  clear(): void {
    this.state = 'EMPTY'
    this.current = null
    this.queue.length = 0
    this.version = 0
    this.ringHead = -1
    this.presetsWanted = null
    this.smoother.clear()
  }

  /** C08: server presets model after a hash mismatch */
  setPresets(model: PresetsModel): void {
    this.P = model
    this.presetsWanted = null
    this.presetsMismatch = model.sha256 !== PRESETS_MODEL.sha256
    // re-decode queued frames with the new model (routes and constants)
    for (let i = 0; i < this.queue.length; i++) this.queue[i] = decodeKeyframe(this.queue[i].wire, model)
    if (this.current) this.current = decodeKeyframe(this.current.wire, model)
  }

  /** global WS epoch from the time frame (C06) */
  onEpoch(epoch: number): void {
    if (epoch === this.globalEpoch) return
    const first = this.globalEpoch < 0
    this.globalEpoch = epoch
    if (!first && this.state !== 'EMPTY') {
      this.state = 'EPOCH_WAIT'
      this.queue.length = 0
    }
  }

  ingest(w: EnvKeyframeWire, nowMs: number): void {
    this.lastRecvMs = nowMs
    const sha = w.config?.presets_sha256
    if (sha && sha !== this.P.sha256) {
      this.presetsWanted = sha
      this.presetsMismatch = true
    }
    const kf = decodeKeyframe(w, this.P)
    const cur = this.current
    if (this.state === 'EMPTY' || this.state === 'EPOCH_WAIT' || (cur && kf.epoch !== cur.epoch)) {
      // C01, C07, producer epoch change: snap everything to this frame
      this.queue.length = 0
      this.queue.push(kf)
      this.current = null
      this.state = 'SYNCED'
      return
    }
    if (this.state === 'STALE') this.state = 'SYNCED'
    const last = this.queue.length ? this.queue[this.queue.length - 1] : cur
    if (last && kf.version < last.version) return
    if (last && kf.version === last.version) {
      // C03 heartbeat of a known version: refresh and check anchors
      if (last === cur) this.checkHeartbeat(kf, nowMs)
      return
    }
    this.queue.push(kf)
    while (this.queue.length > QUEUE_MAX) this.queue.shift()
  }

  /** heartbeat anchors vs the local grid value at the same time: smooth below the threshold, snap above */
  private checkHeartbeat(hb: EnvKeyframe, nowMs: number): void {
    const t = hb.anchors.tNs
    const local = this.localAt(t)
    if (!local) return
    const e = this.err
    e[0] = hb.anchors.sM - local.sM
    e[1] = hb.anchors.d[0] - local.d[0]
    e[2] = hb.anchors.d[1] - local.d[1]
    e[3] = hb.anchors.d[2] - local.d[2]
    e[4] = hb.anchors.fallRain - local.fallRain
    e[5] = hb.anchors.fallSnow - local.fallSnow
    e[6] = hb.anchors.wetness - local.wetness
    e[7] = hb.anchors.puddle - local.puddle
    let m = 0
    for (let i = 0; i < 6; i++) m = Math.max(m, Math.abs(e[i]))
    this.anchorDriftM = m
    const w = Math.max(Math.abs(e[6]), Math.abs(e[7]))
    if (m < 1e-9 && w < 1e-12) return
    if (m < this.opts.snapM && w < this.opts.snapWetness) {
      // true values move to the heartbeat; the visual offset starts at -e and decays (no visible jump)
      this.applyErr(e, 1)
      for (let i = 0; i < 8; i++) e[i] = -e[i]
      this.smoother.begin(e, nowMs)
    } else {
      this.applyErr(e, 1)
      this.smoother.clear()
    }
  }

  private applyErr(e: Float64Array, k: number): void {
    const A = this.A
    A.sM += k * e[0]
    A.d[0] += k * e[1]
    A.d[1] += k * e[2]
    A.d[2] += k * e[3]
    A.fallRain += k * e[4]
    A.fallSnow += k * e[5]
    A.wetness += k * e[6]
    A.puddle += k * e[7]
    this.ringHead = -1
  }

  /** local grid anchors at t (t within the ring or ahead of A), null when unknown */
  private localAt(t: number): Anchors | null {
    const cur = this.current
    if (!cur) return null
    const k = Math.floor(t / H_NS)
    if (k >= this.kA) {
      if (k - this.kA > MAX_STEPS_PER_FRAME) return null
      copyAnchors(this.A, this.tmpA)
      advance(this.tmpA, cur, this.kA, k)
      return this.tmpA
    }
    const r = this.fromRing(k)
    return r
  }

  private fromRing(k: number): Anchors | null {
    if (this.ringHead < 0) return null
    for (let i = 0; i < RING; i++) {
      const a = this.ring[i]
      if (a.tNs === k * H_NS && a.tNs <= this.A.tNs) return a
    }
    return null
  }

  private pushRing(): void {
    this.ringHead = (this.ringHead + 1) % RING
    copyAnchors(this.A, this.ring[this.ringHead])
  }

  /** make kf current at the grid of its t_apply (or at tRender when the frame is an initial snap) */
  private switchTo(kf: EnvKeyframe, tNs: number, nowMs: number, snap: boolean): void {
    const prev = this.current
    const kR = Math.floor(tNs / H_NS)
    const fa = kf.anchors
    const kF = Math.floor(fa.tNs / H_NS)
    if (snap || !prev) {
      copyAnchors(fa, this.A)
      this.kA = kF
      if (kF > kR) {
        // first frame later than tRender: integrate S, D, fall backwards (wetness held)
        const back = Math.min(kF - kR, MAX_BACK_STEPS)
        backstep(this.A, kf, kF, kF - back)
        this.kA = kF - back
      }
      this.ringHead = -1
      this.smoother.clear()
    } else {
      // forward switch at t_apply: the change frame carries anchors at t_apply
      const kApply = Math.floor(kf.tApplyNs / H_NS)
      if (this.kA < kApply) advance(this.A, prev, this.kA, Math.min(kApply, this.kA + MAX_STEPS_PER_FRAME))
      if (kF === kApply) copyAnchors(fa, this.A)
      this.kA = kApply
      this.A.tNs = kApply * H_NS
    }
    const stepSwitch = !!prev && !snap && kf.mode === MODE_STEP && prev.epoch === kf.epoch
    this.current = kf
    this.version = kf.version
    this.switches++
    if (stepSwitch) this.damper.start(nowMs)
    else if (snap) this.damper.active = false
  }

  /** per frame (world phase): tRenderS seconds since the session start, nowMs wall clock */
  update(tRenderS: number, nowMs: number): void {
    if (this.state === 'SYNCED' && nowMs - this.lastRecvMs > this.opts.staleWallMs) this.state = 'STALE'
    if (this.state === 'EMPTY' || this.state === 'EPOCH_WAIT') return
    const tNs = tRenderS * 1e9
    this.tRenderNs = tNs
    const q = this.queue
    let snapped = false
    if (!this.current && q.length) {
      // C01/C07: highest queued version whose t_apply <= tRender, else the oldest queued (eval uses `from` before t0)
      let pick = 0
      for (let i = 0; i < q.length; i++) if (q[i].tApplyNs <= tNs) pick = i
      const kf = q[pick]
      q.splice(0, pick + 1)
      this.switchTo(kf, tNs, nowMs, true)
      snapped = true
    }
    while (q.length && q[0].tApplyNs <= tNs) {
      const kf = q.shift()!
      this.switchTo(kf, tNs, nowMs, false)
    }
    const cur = this.current
    if (!cur) return
    // anchors: grid integration to floor(tRender / H)
    const k = Math.floor(tNs / H_NS)
    if (k > this.kA) {
      const to = Math.min(k, this.kA + MAX_STEPS_PER_FRAME)
      for (let kk = this.kA; kk < to; kk++) {
        advance(this.A, cur, kk, kk + 1)
        if (to - kk <= RING) this.pushRing()
      }
      this.kA = to
    } else if (k < this.kA) {
      const r = this.fromRing(k)
      if (r) {
        copyAnchors(r, this.A)
        this.kA = k
      } else if (Math.floor(cur.anchors.tNs / H_NS) <= k) {
        copyAnchors(cur.anchors, this.A)
        this.kA = Math.floor(cur.anchors.tNs / H_NS)
        advance(this.A, cur, this.kA, Math.min(k, this.kA + MAX_STEPS_PER_FRAME))
        this.kA = Math.min(k, this.kA + MAX_STEPS_PER_FRAME)
        this.ringHead = -1
      } else if (this.kA - k <= MAX_BACK_STEPS) {
        backstep(this.A, cur, this.kA, k)
        this.kA = k
        this.ringHead = -1
      }
    }
    if (tNs >= this.kA * H_NS && tNs - this.kA * H_NS < H_NS) partial(this.A, cur, tNs, this.anchors)
    else copyAnchors(this.A, this.anchors)
    evalEnv(cur, tNs, this.scalars)
    derive(this.scalars, this.derived, cur.profile, cur.P)
    if (snapped) this.damper.snap(this.scalars)
    else this.damper.update(this.scalars, nowMs, cur.P)
    derive(this.damper.vis, this.derivedVis, cur.profile, cur.P)
    const off = this.smoother.update(nowMs)
    const a = this.anchors
    const v = this.anchorsVis
    v.tNs = a.tNs
    v.sM = a.sM + off[0]
    v.d[0] = a.d[0] + off[1]
    v.d[1] = a.d[1] + off[2]
    v.d[2] = a.d[2] + off[3]
    v.fallRain = a.fallRain + off[4]
    v.fallSnow = a.fallSnow + off[5]
    v.wetness = a.wetness + off[6]
    v.puddle = a.puddle + off[7]
  }

  /** transition window of the current keyframe in sim seconds and progress (0..1) at tRender */
  transition(out: { active: boolean; t0SimS: number; t1SimS: number; progress: number }): typeof out {
    const cur = this.current
    if (!cur || cur.mode === MODE_STEP) {
      out.active = false
      out.t0SimS = cur ? cur.t0Ns / 1e9 : 0
      out.t1SimS = out.t0SimS
      out.progress = 1
      return out
    }
    out.t0SimS = cur.t0Ns / 1e9
    out.t1SimS = cur.t1Ns / 1e9
    const x = (this.tRenderNs - cur.t0Ns) / Math.max(1, cur.t1Ns - cur.t0Ns)
    out.progress = Math.min(Math.max(x, 0), 1)
    out.active = this.tRenderNs < cur.t1Ns
    return out
  }

  /** preset shown as selected: the keyframe target, else the preset the scalars match exactly */
  activePreset(): string | null {
    const cur = this.current
    if (!cur) return null
    return cur.toPreset ?? this.P.matchPreset(this.scalars)
  }
}
