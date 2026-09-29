// sensorCache (M13-FR-021, FR-022; M13 §6.4.3, §6.5.12). Owner: M13. Per agent number: the SensorViews of the sensors
// with a field of view (camera, thermal, lidar), built once from the vehicle's sensor files (specs.gen.ts, the same values
// as GET /api/fleet/profiles/{id} sensors{}; setModelSpecs() overrides them with the REST document), plus a ring of the
// newest 4 SensorPose48 samples [t_ms, qx, qy, qz, qw, px, py, pz, flags]. A sample is turned into gimbal target angles
// with the body orientation at the sample time (orientation source: injected interpolator, else the swarm/Full64 history
// kept here); samples older than 3 / f leave FOV_VALID = 0 (the frustum turns dashed). Views are allocated once per agent,
// the per-frame paths write in place (zero allocation). Memory: about 0.6 KB per view (<= 64 KB for the Tier B/A 16 + 2).
import { type GimbalState, dampGimbal, gimbalFromSample, nlerp } from './gimbalTrack'
import type { SensorGeom } from './intrinsics'
import { SENSOR_SPECS, type SensorSpecJson } from './specs.gen'

export const RING = 4
export const STRIDE = 9
export const POSE_HZ = 10
export const STALE_INTERVALS = 3
export const FLAG_ACTIVE = 1
export const FLAG_FOV_VALID = 2
/** SensorPose48 kind values (rt/enums.json SensorKind) */
export const KIND = { CAMERA: 0, LIDAR: 1, THERMAL: 2, RADAR: 3 } as const
const D2R = Math.PI / 180

/** M13 §6.4.3 SensorView (superset of the fields M06 reads in engine/drones/frustums.ts) */
export interface SensorView extends SensorGeom {
  agentNo: number
  sensorNo: number
  name: string
  /** SensorPose48 kind: 0 camera, 1 lidar, 2 thermal, 3 radar */
  kind: number
  hfov: number
  vfov: number
  rangeM: number
  rateHz: number
  conf: string
  capability: string | null
  gimbal: GimbalState
  /** az min, az max, el min, el max (rad) */
  limits: Float64Array
  defaultAz: number
  defaultEl: number
  /** 4 x [t_ms, qx, qy, qz, qw, px, py, pz, flags] */
  ring: Float64Array
  ringHead: number
  /** at least one sample received */
  valid: boolean
  /** newest sample fresh (<= 3 / f) and FOV_VALID set; true before the first sample so the static frustum shows */
  fovValid: boolean
  active: boolean
  /** age of the newest sample in simulation seconds (NaN before the first) */
  poseAgeS: number
  /** sample time whose gimbal angles still wait for the body orientation (NaN when none) */
  pendingT: number
  pendingTries: number
}

/** body orientation at a simulation time: out = [x, y, z, w]; false when unknown */
export type OrientationSource = (agentNo: number, tMs: number, out: Float64Array) => boolean

const EMPTY: readonly SensorView[] = Object.freeze([])
const H = 8 // orientation history length per agent

function viewOf(agentNo: number, sp: SensorSpecJson): SensorView {
  const g = sp.gimbal
  const lim = new Float64Array(4)
  if (g) {
    lim[0] = g.yaw_min_deg * D2R
    lim[1] = g.yaw_max_deg * D2R
    lim[2] = g.pitch_min_deg * D2R
    lim[3] = g.pitch_max_deg * D2R
  }
  const az = g ? g.default_yaw_deg * D2R : 0
  const el = g ? g.default_pitch_deg * D2R : 0
  return {
    agentNo, sensorNo: sp.sensorNo, name: sp.name, kind: sp.kindCode,
    w: sp.w, h: sp.h, fx: sp.fx, fy: sp.fy, cx: sp.cx, cy: sp.cy,
    hfov: sp.hfov ?? Number.NaN, vfov: sp.vfov ?? Number.NaN, rangeM: sp.rangeM ?? Number.NaN, rateHz: sp.rateHz ?? Number.NaN,
    conf: sp.conf ?? '', capability: sp.capability, mount: Float64Array.from(sp.mount),
    hasGimbal: g !== null, gimbal: { az, el, azT: az, elT: el, vaz: 0, vel: 0 }, limits: lim, defaultAz: az, defaultEl: el,
    ring: new Float64Array(RING * STRIDE).fill(Number.NaN), ringHead: 0, valid: false, fovValid: true, active: true,
    poseAgeS: Number.NaN, pendingT: Number.NaN, pendingTries: 0,
  }
}

export class SensorCache {
  private readonly views = new Map<number, SensorView[]>()
  private readonly models = new Map<number, string>()
  private readonly overrides = new Map<string, readonly SensorSpecJson[]>()
  /** agent -> ring of [t, x, y, z, w] body orientations (history for the gimbal derivation) */
  private readonly orient = new Map<number, { buf: Float64Array; head: number; n: number }>()
  private readonly tmp4 = new Float64Array(4)
  private readonly tmp2 = new Float64Array(2)
  private readonly qws = new Float64Array(4)
  orientationSource: OrientationSource | null = null
  /** agent number -> roster model (vehicle directory), injected by the facade */
  modelOf: (agentNo: number) => string | undefined = () => undefined
  samples = 0
  resolved = 0
  fallbacks = 0

  specsFor(model: string): readonly SensorSpecJson[] {
    return this.overrides.get(model) ?? SENSOR_SPECS[model] ?? EMPTY_SPECS
  }

  /** REST sensors{} (GET /api/fleet/profiles/{id}) for a model: replaces the generated specs of that model */
  setModelSpecs(model: string, specs: readonly SensorSpecJson[]): void {
    this.overrides.set(model, specs)
    for (const [a, m] of this.models) if (m === model) this.views.delete(a)
  }

  sensorsOf(agentNo: number): readonly SensorView[] {
    const v = this.views.get(agentNo)
    if (v) return v
    const model = this.modelOf(agentNo)
    if (model === undefined) return EMPTY
    const list = this.specsFor(model)
      .filter((s) => s.kindCode <= KIND.RADAR)
      .map((s) => viewOf(agentNo, s))
    this.views.set(agentNo, list)
    this.models.set(agentNo, model)
    return list
  }

  hasCamera(agentNo: number): boolean {
    const l = this.sensorsOf(agentNo)
    for (let i = 0; i < l.length; i++) if (l[i].kind === KIND.CAMERA && l[i].w > 0) return true
    return false
  }

  viewBySensorNo(agentNo: number, sensorNo: number): SensorView | null {
    const l = this.sensorsOf(agentNo)
    for (let i = 0; i < l.length; i++) if (l[i].sensorNo === sensorNo) return l[i]
    return null
  }

  forget(agentNo: number): void {
    this.views.delete(agentNo)
    this.models.delete(agentNo)
    this.orient.delete(agentNo)
  }

  clear(): void {
    this.views.clear()
    this.models.clear()
    this.orient.clear()
  }

  agents(): IterableIterator<number> {
    return this.views.keys()
  }

  /** body orientation sample (swarm or Full64) for the gimbal derivation; only agents with views are kept */
  pushOrientation(agentNo: number, tMs: number, qx: number, qy: number, qz: number, qw: number): void {
    if (!this.views.has(agentNo)) return
    let h = this.orient.get(agentNo)
    if (!h) {
      h = { buf: new Float64Array(H * 5), head: 0, n: 0 }
      this.orient.set(agentNo, h)
    }
    const last = h.n > 0 ? h.buf[((h.head - 1 + H) % H) * 5] : Number.NEGATIVE_INFINITY
    if (tMs < last) return
    const o = (tMs === last ? (h.head - 1 + H) % H : h.head) * 5
    h.buf[o] = tMs
    h.buf[o + 1] = qx
    h.buf[o + 2] = qy
    h.buf[o + 3] = qz
    h.buf[o + 4] = qw
    if (tMs !== last) {
      h.head = (h.head + 1) % H
      h.n = Math.min(H, h.n + 1)
    }
  }

  /** body orientation at tMs from the history: interpolated when bracketed, nearest when `nearest`, else false */
  orientationAt(agentNo: number, tMs: number, out: Float64Array, nearest: boolean): boolean {
    if (this.orientationSource && this.orientationSource(agentNo, tMs, out)) return true
    const h = this.orient.get(agentNo)
    if (!h || h.n === 0) return false
    let bi = -1
    let ai = -1
    for (let k = 0; k < h.n; k++) {
      const i = (h.head - 1 - k + 2 * H) % H
      const t = h.buf[i * 5]
      if (t >= tMs) ai = i
      else {
        bi = i
        break
      }
    }
    const b = h.buf
    if (ai >= 0 && bi >= 0) {
      const t0 = b[bi * 5]
      const t1 = b[ai * 5]
      const u = t1 > t0 ? (tMs - t0) / (t1 - t0) : 0
      nlerp(b, bi * 5 + 1, b, ai * 5 + 1, u, out)
      return true
    }
    if (ai >= 0 && b[ai * 5] === tMs) {
      for (let i = 0; i < 4; i++) out[i] = b[ai * 5 + 1 + i]
      return true
    }
    if (!nearest) return false
    const i = ai >= 0 ? ai : bi
    for (let k = 0; k < 4; k++) out[k] = b[i * 5 + 1 + k]
    return true
  }

  /** one SensorPose48 sample (M13 §7.2.2): world position and [x, y, z, w] WORLD<-SENSOR of the sensor */
  onPose(agentNo: number, sensorNo: number, tMs: number, flags: number, px: number, py: number, pz: number,
    qx: number, qy: number, qz: number, qw: number): SensorView | null {
    const v = this.viewBySensorNo(agentNo, sensorNo)
    if (!v) return null
    const newest = v.valid ? v.ring[((v.ringHead - 1 + RING) % RING) * STRIDE] : Number.NEGATIVE_INFINITY
    if (tMs < newest) return v
    // quaternion sign continuity with the previous sample (M13 §9.1)
    if (v.valid) {
      const o = ((v.ringHead - 1 + RING) % RING) * STRIDE
      if (v.ring[o + 1] * qx + v.ring[o + 2] * qy + v.ring[o + 3] * qz + v.ring[o + 4] * qw < 0) {
        qx = -qx
        qy = -qy
        qz = -qz
        qw = -qw
      }
    }
    const o = (tMs === newest ? (v.ringHead - 1 + RING) % RING : v.ringHead) * STRIDE
    const r = v.ring
    r[o] = tMs
    r[o + 1] = qx
    r[o + 2] = qy
    r[o + 3] = qz
    r[o + 4] = qw
    r[o + 5] = px
    r[o + 6] = py
    r[o + 7] = pz
    r[o + 8] = flags
    if (tMs !== newest) v.ringHead = (v.ringHead + 1) % RING
    v.valid = true
    v.active = (flags & FLAG_ACTIVE) !== 0
    this.samples++
    if (v.hasGimbal) {
      v.pendingT = tMs
      v.pendingTries = 0
      this.resolve(v, false)
    }
    return v
  }

  /** gimbal targets of the pending sample; after 3 attempts the nearest body orientation is used (RK-6) */
  private resolve(v: SensorView, force: boolean): void {
    if (!Number.isFinite(v.pendingT)) return
    if (!this.orientationAt(v.agentNo, v.pendingT, this.tmp4, force)) {
      v.pendingTries++
      return
    }
    const o = this.sampleIndex(v, v.pendingT)
    if (o < 0) {
      v.pendingT = Number.NaN
      return
    }
    const r = v.ring
    this.qws[0] = r[o + 1]
    this.qws[1] = r[o + 2]
    this.qws[2] = r[o + 3]
    this.qws[3] = r[o + 4]
    if (gimbalFromSample(this.qws, this.tmp4, v.mount, this.tmp2)) {
      const g = v.gimbal
      g.azT = Math.min(Math.max(this.tmp2[0], v.limits[0]), v.limits[1])
      g.elT = Math.min(Math.max(this.tmp2[1], v.limits[2]), v.limits[3])
      if (force) this.fallbacks++
      else this.resolved++
    }
    v.pendingT = Number.NaN
  }

  private sampleIndex(v: SensorView, tMs: number): number {
    for (let k = 0; k < RING; k++) if (v.ring[k * STRIDE] === tMs) return k * STRIDE
    return -1
  }

  /**
   * World phase (M13 §7.4 updateGimbals): pending gimbal derivations, critically damped follow, staleness.
   * tRenderMs is the render time in simulation ms; reduced jumps to the targets.
   */
  update(dtMs: number, tRenderMs: number, reduced: boolean): void {
    const dt = dtMs / 1000
    const staleMs = (STALE_INTERVALS * 1000) / POSE_HZ
    for (const list of this.views.values()) {
      for (let i = 0; i < list.length; i++) {
        const v = list[i]
        if (Number.isFinite(v.pendingT)) this.resolve(v, v.pendingTries >= 3)
        if (v.hasGimbal) dampGimbal(v.gimbal, dt, reduced)
        if (!v.valid) {
          v.poseAgeS = Number.NaN
          v.fovValid = true
          continue
        }
        const o = ((v.ringHead - 1 + RING) % RING) * STRIDE
        const age = tRenderMs - v.ring[o]
        v.poseAgeS = age / 1000
        v.fovValid = age <= staleMs && (v.ring[o + 8] & FLAG_FOV_VALID) !== 0
      }
    }
  }

  /** approximate bytes held (M13-NFR-008: <= 64 KB) */
  bytes(): number {
    let n = 0
    for (const l of this.views.values()) n += l.length * (RING * STRIDE * 8 + 16 * 8 + 4 * 8 + 256)
    return n + this.orient.size * (H * 5 * 8 + 64)
  }
}

const EMPTY_SPECS: readonly SensorSpecJson[] = Object.freeze([])
