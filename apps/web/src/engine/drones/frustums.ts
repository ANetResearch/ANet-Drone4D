// Sensor frustums, SensorLayer (M06-FR-046, AC-035; AWR-15 §10.7; M13 §7.4, §8.1). Owner: M06.
// The sensor geometry is M13's (engine/sensors/intrinsics.ts, injected as SensorsApi): frustumCorners(sensor, L, out)
// gives origin + 4 far corners in the body frame (mount and smoothed gimbal applied), L = min(range_m, 60 m) (60 m when
// range is NaN). M06 multiplies by the vehicle pose at tRender (tFocus for the FPV focus vehicle), in the world phase
// after m13.gimbal. Edges: 8 segments (origin to 4 corners, far rectangle), 1 px --fov-edge g300 70 %, dashed when the
// newest SensorPose48 sample is older than 3 / f; far plane: 2 triangles g50 5 %, collapsed when FOV_VALID = 0;
// sensors with ACTIVE = 0 are skipped. Tier S draws the selected vehicle only, Tier B/A <= 16. PerfGovernor step 2
// (selected only, then off) fades the edges out over --duration-quick (uniform, no recompile).
// D2 (WP-11, M13-FR-217 request item 2): the 850 nm illuminators of the primary vehicle's payload (the eo built-in one,
// divergence = HFOV, and the external nir_ill_l) are drawn as dashed cones (pyramids of the illuminator's divergence along
// the eo camera axis, length min(R_nir, 60 m), no fill) in up to two extra slots of the same two draws.
import { BufferAttribute, BufferGeometry, DoubleSide, LineSegments, Mesh, Quaternion, Sphere, Vector3 } from 'three'
import { LineBasicNodeMaterial, MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, attribute, float, mod, uniform, varying, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import type { TelemetryFrame } from '@/net/rt/types'
import { hypot3 } from '../hypot'

type N = any // TSL nodes

/** M13 SensorView (M13 §6.4.3), the fields M06 reads */
export interface SensorView {
  agentNo: number
  sensorNo: number
  name: string
  kind: number
  rangeM: number
  hfov: number
  vfov: number
  /** 4 x [t_ms, qx, qy, qz, qw, px, py, pz, flags] */
  ring: Float64Array
  ringHead: number
  valid: boolean
  w?: number
  h?: number
  /** D2 (M13-FR-217): optical zoom geometry (null for fixed lenses) and the drawn focal length (mm, NaN when fixed) */
  zoom?: unknown
  fMm?: number
}
/** the fields of uav/{id}/payload (awr.uav.payload.v1, M13) the sensor views read: imaging modes and illuminators */
export interface PayloadView {
  sensors?: readonly {
    sensor: string
    kind?: string
    active?: boolean
    enabled?: boolean
    mode?: string | null
    palette?: string | null
    hfov_rad?: number | null
    illuminator?: { on: boolean; kind?: string; divergence_rad?: number; range_m?: number } | null
  }[]
}
/** M13 engine/sensors/intrinsics.ts (M13 §7.4) */
export interface SensorsApi {
  sensorsOf(agentNo: number): readonly SensorView[]
  hasCamera(agentNo: number): boolean
  frustumCorners(s: SensorView, L: number, out: Float64Array): Float64Array
  T_base_cam(s: SensorView, out: Float64Array): Float64Array
  projectionFor(s: SensorView, aspect: number, near: number, far: number, out: Float64Array): Float64Array
  frameRect?(s: SensorView, aspect: number, out: Float64Array): Float64Array
  /**
   * SensorPose48 samples and body orientations of this frame's TelemetryFrame (M13-to-M06 item 1): the telemetry phase
   * calls it right after RtClient.swapFrame() and time.ingest(); returns the number of pose records taken
   */
  ingestFrame?(f: TelemetryFrame): number
  /** body orientation source for the gimbal derivation (M12 interpolation; M13 falls back to its own history) */
  setOrientationSource?(fn: ((agentNo: number, tMs: number, out4: Float64Array) => boolean) | null): void
  /** motion tier of the page (reduced drops the gimbal damping) */
  setMotionTier?(fn: (() => 'full' | 'lite' | 'reduced') | null): void
  /** D2: latest uav/{id}/payload of an agent (M13-FR-217), read by the night view for the illuminators */
  payloadOf?(agentNo: number): PayloadView | undefined
}

export const FRUSTUM = { maxLenM: 60, capS: 1, capBA: 16, staleIntervals: 3, poseHz: 10, dashM: 1.5, gapM: 1.5, illumSlots: 2 } as const
export const SENSOR_ACTIVE = 1
export const SENSOR_FOV_VALID = 2

const EDGE_VERTS = 16
const FILL_VERTS = 6

/** frustum length L = min(range, 60 m); NaN or non-positive range -> 60 m */
export function frustumLength(rangeM: number): number {
  return Number.isFinite(rangeM) && rangeM > 0 ? Math.min(rangeM, FRUSTUM.maxLenM) : FRUSTUM.maxLenM
}

/** an 850 nm illuminator of a payload: divergence (full angle, rad) and the effective range R_nir (m) */
export interface IlluminatorView { on: boolean; divergenceRad: number; rangeM: number }
const NIR_ILL_DIV_RAD = (5.5 * Math.PI) / 180 // nir_ill_l (AWR-04 §6.1)
const NIR_ILL_RANGE_M = 464.8

/**
 * the illuminators switched on in a payload (uav/{id}/payload, M13): an imaging entry's illuminator{on, divergence_rad,
 * range_m} (the eo built-in one, divergence = current HFOV) and nir_illuminator entries; at most out.length; returns the count
 */
export function payloadIlluminators(pl: PayloadView | undefined, out: IlluminatorView[]): number {
  let k = 0
  const sens = pl?.sensors ?? []
  for (let i = 0; i < sens.length && k < out.length; i++) {
    const s = sens[i]
    const il = s.illuminator
    let div = Number.NaN
    let range = Number.NaN
    if (il && il.on) {
      div = il.divergence_rad ?? s.hfov_rad ?? NIR_ILL_DIV_RAD
      range = il.range_m ?? NIR_ILL_RANGE_M
    } else if (!il && s.kind === 'nir_illuminator' && (s.active === true || s.enabled === true)) {
      div = NIR_ILL_DIV_RAD
      range = NIR_ILL_RANGE_M
    }
    if (!(div > 0) || !(range > 0)) continue
    out[k].on = true
    out[k].divergenceRad = div
    out[k].rangeM = range
    k++
  }
  return k
}

/** newest SensorPose48 sample (t_ms, flags) into out; false when there is none */
export function newestSample(s: SensorView, out: { tMs: number; flags: number }): boolean {
  if (!s.valid || s.ring.length < 9) return false
  const k = (s.ringHead - 1 + 4) % 4
  const o = 9 * k
  const t = s.ring[o]
  if (!Number.isFinite(t)) return false
  out.tMs = t
  out.flags = s.ring[o + 8]
  return true
}

export class FrustumLayer {
  readonly edges: LineSegments
  readonly fill: Mesh
  private readonly ePos: BufferAttribute
  private readonly eDash: BufferAttribute
  private readonly eDist: BufferAttribute
  private readonly fPos: BufferAttribute
  private readonly corners = new Float64Array(15)
  private readonly v = new Vector3()
  private readonly q = new Quaternion()
  private readonly uEdgeAlpha: N
  private readonly uFillAlpha: N
  count = 0
  /** multiplier for the governor fade (1 visible, 0 hidden) */
  fade = 1

  constructor(readonly cap: number, readonly extra: number = FRUSTUM.illumSlots) {
    const eg = new BufferGeometry()
    const slots = cap + extra
    this.ePos = new BufferAttribute(new Float32Array(slots * EDGE_VERTS * 3), 3)
    this.eDash = new BufferAttribute(new Float32Array(slots * EDGE_VERTS), 1)
    this.eDist = new BufferAttribute(new Float32Array(slots * EDGE_VERTS), 1)
    for (const a of [this.ePos, this.eDash, this.eDist]) a.setUsage(35048)
    eg.setAttribute('position', this.ePos)
    eg.setAttribute('dashed', this.eDash)
    eg.setAttribute('lineDistance', this.eDist)
    eg.boundingSphere = new Sphere(new Vector3(), 1e7)
    eg.setDrawRange(0, 0)
    const em = new LineBasicNodeMaterial()
    const ec = SCENE.fovEdge
    this.uEdgeAlpha = uniform(ec.a)
    const dashed: N = varying(attribute('dashed', 'float'))
    const dist: N = varying(attribute('lineDistance', 'float'))
    em.colorNode = vec4(ec.rgb[0], ec.rgb[1], ec.rgb[2], 1) as N
    em.opacityNode = Fn(() => {
      dashed.greaterThan(0.5).and(mod(dist, float(FRUSTUM.dashM + FRUSTUM.gapM)).greaterThan(FRUSTUM.dashM)).discard()
      return this.uEdgeAlpha
    })() as N
    em.transparent = true
    em.depthWrite = false
    em.fog = false
    this.edges = new LineSegments(eg, em)
    this.edges.frustumCulled = false
    this.edges.renderOrder = 50
    this.edges.name = 'FrustumEdges'
    this.edges.visible = false
    const fg = new BufferGeometry()
    this.fPos = new BufferAttribute(new Float32Array(slots * FILL_VERTS * 3), 3)
    this.fPos.setUsage(35048)
    fg.setAttribute('position', this.fPos)
    fg.boundingSphere = new Sphere(new Vector3(), 1e7)
    fg.setDrawRange(0, 0)
    const fm = new MeshBasicNodeMaterial()
    const fc = SCENE.fovFill
    this.uFillAlpha = uniform(fc.a)
    fm.colorNode = vec4(fc.rgb[0], fc.rgb[1], fc.rgb[2], 1) as N
    fm.opacityNode = this.uFillAlpha
    fm.transparent = true
    fm.depthWrite = false
    fm.side = DoubleSide
    fm.forceSinglePass = true // one draw per frame: transparent DoubleSide otherwise renders twice (M06-E007; INT-1)
    fm.fog = false
    this.fill = new Mesh(fg, fm)
    this.fill.frustumCulled = false
    this.fill.renderOrder = 50
    this.fill.name = 'FrustumFill'
    this.fill.visible = false
  }

  /** begin a frame's frustum list */
  begin(): void {
    this.count = 0
  }

  /**
   * add one sensor frustum of a vehicle at pose (ENU position, quaternion [x, y, z, w] WORLD<-FLU).
   * Returns false when the layer is full or the sensor is inactive.
   */
  add(api: SensorsApi, s: SensorView, px: number, py: number, pz: number, qx: number, qy: number, qz: number, qw: number, tRenderS: number): boolean {
    if (this.count >= this.cap) return false
    const has = newestSample(s, this.smp)
    const flags = has ? this.smp.flags : SENSOR_ACTIVE | SENSOR_FOV_VALID
    if ((flags & SENSOR_ACTIVE) === 0) return false
    const stale = !has || tRenderS * 1000 - this.smp.tMs > (FRUSTUM.staleIntervals * 1000) / FRUSTUM.poseHz
    const c = api.frustumCorners(s, frustumLength(s.rangeM), this.corners)
    this.write(c, px, py, pz, qx, qy, qz, qw, stale, (flags & SENSOR_FOV_VALID) !== 0)
    return true
  }

  /**
   * D2: an illuminator cone along the camera s (its pose and gimbal): the camera's far rectangle rescaled about its centre
   * to the divergence (half diagonal = L tan(div / 2) sqrt(2)), L = min(R_nir, 60 m); dashed edges, no fill.
   * Uses the extra slots after the sensor frustums; false when they are full.
   */
  addIlluminator(api: SensorsApi, s: SensorView, divergenceRad: number, rangeM: number, px: number, py: number, pz: number,
    qx: number, qy: number, qz: number, qw: number): boolean {
    if (this.count >= this.cap + this.extra || !(divergenceRad > 0)) return false
    const L = frustumLength(rangeM)
    const c = api.frustumCorners(s, L, this.corners)
    let ax = 0, ay = 0, az = 0
    for (let k = 1; k <= 4; k++) {
      ax += c[3 * k] / 4
      ay += c[3 * k + 1] / 4
      az += c[3 * k + 2] / 4
    }
    const len = hypot3(ax - c[0], ay - c[1], az - c[2])
    const target = len * Math.tan(Math.min(divergenceRad, 3) / 2) * Math.SQRT2
    for (let k = 1; k <= 4; k++) {
      const dx = c[3 * k] - ax, dy = c[3 * k + 1] - ay, dz = c[3 * k + 2] - az
      const r = hypot3(dx, dy, dz) || 1
      c[3 * k] = ax + (dx * target) / r
      c[3 * k + 1] = ay + (dy * target) / r
      c[3 * k + 2] = az + (dz * target) / r
    }
    this.write(c, px, py, pz, qx, qy, qz, qw, true, false)
    return true
  }

  private write(c: Float64Array, px: number, py: number, pz: number, qx: number, qy: number, qz: number, qw: number, dashed: boolean, fov: boolean): void {
    this.q.set(qx, qy, qz, qw)
    const P = this.ePos.array as Float32Array
    const D = this.eDash.array as Float32Array
    const L = this.eDist.array as Float32Array
    const F = this.fPos.array as Float32Array
    const eo = this.count * EDGE_VERTS
    const fo = this.count * FILL_VERTS
    // world points: origin 0, corners 1..4
    const w = this.worldPts
    for (let k = 0; k < 5; k++) {
      this.v.set(c[3 * k], c[3 * k + 1], c[3 * k + 2]).applyQuaternion(this.q)
      w[3 * k] = px + this.v.x
      w[3 * k + 1] = py + this.v.y
      w[3 * k + 2] = pz + this.v.z
    }
    const seg = FrustumLayer.SEGS
    for (let e = 0; e < 8; e++) {
      const a = seg[2 * e]
      const b = seg[2 * e + 1]
      const i0 = eo + 2 * e
      for (let j = 0; j < 3; j++) {
        P[3 * i0 + j] = w[3 * a + j]
        P[3 * (i0 + 1) + j] = w[3 * b + j]
      }
      D[i0] = D[i0 + 1] = dashed ? 1 : 0
      L[i0] = 0
      L[i0 + 1] = hypot3(w[3 * b] - w[3 * a], w[3 * b + 1] - w[3 * a + 1], w[3 * b + 2] - w[3 * a + 2])
    }
    const tri = FrustumLayer.FILL
    for (let k = 0; k < 6; k++) {
      const src = fov ? tri[k] : 1
      for (let j = 0; j < 3; j++) F[3 * (fo + k) + j] = w[3 * src + j]
    }
    this.count++
  }

  commit(): void {
    const n = this.count
    this.edges.geometry.setDrawRange(0, n * EDGE_VERTS)
    this.fill.geometry.setDrawRange(0, n * FILL_VERTS)
    const vis = n > 0 && this.fade > 0.001
    this.edges.visible = vis
    this.fill.visible = vis
    this.uEdgeAlpha.value = SCENE.fovEdge.a * this.fade
    this.uFillAlpha.value = SCENE.fovFill.a * this.fade
    if (n > 0) {
      this.ePos.needsUpdate = true
      this.eDash.needsUpdate = true
      this.eDist.needsUpdate = true
      this.fPos.needsUpdate = true
    }
  }

  /** last written world corners of frustum k (tests): origin + 4 corners */
  cornersOf(k: number, out: Float64Array): Float64Array {
    const P = this.ePos.array as Float32Array
    const eo = k * EDGE_VERTS
    // edge e = 0..3 are origin -> corner e+1
    for (let j = 0; j < 3; j++) out[j] = P[3 * eo + j]
    for (let e = 0; e < 4; e++) for (let j = 0; j < 3; j++) out[3 * (e + 1) + j] = P[3 * (eo + 2 * e + 1) + j]
    return out
  }

  drawCount(): number {
    return (this.edges.visible ? 1 : 0) + (this.fill.visible ? 1 : 0)
  }

  dispose(): void {
    this.edges.geometry.dispose()
    ;(this.edges.material as LineBasicNodeMaterial).dispose()
    this.fill.geometry.dispose()
    ;(this.fill.material as MeshBasicNodeMaterial).dispose()
  }

  private readonly worldPts = new Float64Array(15)
  private readonly smp = { tMs: 0, flags: 0 }
  /** origin->c1..c4, then the far rectangle c1c2, c2c3, c3c4, c4c1 */
  static readonly SEGS = new Uint8Array([0, 1, 0, 2, 0, 3, 0, 4, 1, 2, 2, 3, 3, 4, 4, 1])
  static readonly FILL = new Uint8Array([1, 2, 3, 1, 3, 4])
}
