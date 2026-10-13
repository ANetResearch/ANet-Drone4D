// MID-360 scan points (D2 WP-11, P1; M13 PRD §6.5.12 "扫描层", §7.2.4 awr.LidarScan8.v1; M13-FR-213; AWR-14 §20.6.5 "扫描
// 显示"; d2-WP-10-to-M06 item 3). Owner: M06.
// uav/{id}/sensor/lidar/scan frames (blob, <= 2 Hz, one vehicle per session) are decoded here and written into a
// preallocated ring of points: position (ENU float32, the sensor frame points rotated by the frame's WORLD<-SENSOR
// quaternion and shifted by its origin), birth time (s) and reflectance (u8). The points fade out with
// alpha = 1 - (t - birth) / persist (persist 0.1-2 s, default 0.6 s), coloured by reflectance through the neutral token
// ramp pcRamp (never red, ADR-032), 2 raster px GL points (1 px where the self test found no gl_PointSize). Ring sizes:
// Tier S 8 000, Tier B/A 200 000 points; a frame larger than half the ring is subsampled evenly. One draw while points are
// alive. Zero allocation per frame; decoding allocates nothing either (DataView over the message).
// Layout (M13 §7.2.4; WP-10 report §1 FR-213): optional 16 B generic blob header ("AWRB", version, dtype, count, comp),
// then t_frame_ns i64, frame_seq u32, n_points u32, pos_enu_m f32 x 3, q_xyzw f32 x 4, decim u16, flags u16 (48 B), then
// n x (x_cm i16, y_cm i16, z_cm i16, refl u8, tag_line u8) in the sensor frame (FLU).
import { BufferAttribute, BufferGeometry, Points, Sphere, Vector3 } from 'three'
import type { PointsNodeMaterial } from 'three/webgpu'
import { Fn, attribute, builtin, clamp, float, mix, uniform, vec3, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'

type N = any // TSL nodes

export const LIDAR_SCAN = { capS: 8000, capBA: 200_000, persistS: 0.6, persistMinS: 0.1, persistMaxS: 2, pointPx: 2, header: 48, blobHeader: 16, point: 8 } as const

export interface LidarScanHeader { tFrameNs: number; seq: number; n: number; pos: Float64Array; q: Float64Array; decim: number; flags: number; offset: number }

const MAGIC_AWRB = 0x42525741 // "AWRB" little endian

/** header of a LidarScan8 message (with or without the generic blob header); false when it is too short or inconsistent */
export function decodeLidarScanHeader(dv: DataView, out: LidarScanHeader): boolean {
  let o = 0
  if (dv.byteLength >= 4 && dv.getUint32(0, true) === MAGIC_AWRB) o = LIDAR_SCAN.blobHeader
  if (dv.byteLength < o + LIDAR_SCAN.header) return false
  out.tFrameNs = Number(dv.getBigInt64(o, true))
  out.seq = dv.getUint32(o + 8, true)
  out.n = dv.getUint32(o + 12, true)
  for (let k = 0; k < 3; k++) out.pos[k] = dv.getFloat32(o + 16 + 4 * k, true)
  for (let k = 0; k < 4; k++) out.q[k] = dv.getFloat32(o + 28 + 4 * k, true)
  out.decim = dv.getUint16(o + 44, true)
  out.flags = dv.getUint16(o + 46, true)
  out.offset = o + LIDAR_SCAN.header
  return dv.byteLength >= out.offset + out.n * LIDAR_SCAN.point
}

export function newLidarScanHeader(): LidarScanHeader {
  return { tFrameNs: 0, seq: 0, n: 0, pos: new Float64Array(3), q: new Float64Array(4), decim: 1, flags: 0, offset: 0 }
}

export class LidarScanLayer {
  readonly points: Points
  readonly cap: number
  private readonly pos: BufferAttribute
  private readonly birth: BufferAttribute
  private readonly refl: BufferAttribute
  private readonly uNow: N
  private readonly uPersist: N
  private readonly hdr = newLidarScanHeader()
  private head = 0
  private used = 0
  /** time of the newest point (s, the caller's clock) */
  private newest = Number.NEGATIVE_INFINITY
  persistS: number = LIDAR_SCAN.persistS
  /** frames ingested and points written (tests, diagnostics) */
  frames = 0
  written = 0

  constructor(cap: number, material: PointsNodeMaterial, pointSizeMode: 'glpoint' | 'pixel' = 'glpoint') {
    this.cap = Math.max(1, cap)
    const g = new BufferGeometry()
    this.pos = new BufferAttribute(new Float32Array(this.cap * 3), 3)
    this.birth = new BufferAttribute(new Float32Array(this.cap), 1)
    this.refl = new BufferAttribute(new Uint8Array(this.cap), 1, true)
    for (const a of [this.pos, this.birth, this.refl]) a.setUsage(35048)
    g.setAttribute('position', this.pos)
    g.setAttribute('birth', this.birth)
    g.setAttribute('refl', this.refl)
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    g.setDrawRange(0, 0)
    this.uNow = uniform(0)
    this.uPersist = uniform(LIDAR_SCAN.persistS)
    const m = material
    const R = SCENE.pcRamp
    const age: N = this.uNow.sub(attribute('birth', 'float'))
    const alpha: N = clamp(float(1).sub(age.div(this.uPersist)), 0, 1)
    m.positionNode = Fn(() => {
      if (pointSizeMode === 'glpoint') builtin('gl_PointSize').assign(float(LIDAR_SCAN.pointPx))
      return attribute('position', 'vec3')
    })() as N
    const r: N = attribute('refl', 'float')
    // reflectance through the neutral ramp, from pcRamp[1] (dark surfaces still visible) to pcRamp[4]
    let c: N = vec3(R[1][0], R[1][1], R[1][2])
    for (let i = 2; i < R.length; i++) c = mix(c, vec3(R[i][0], R[i][1], R[i][2]), clamp(r.mul(R.length - 2).sub(i - 2), 0, 1))
    m.colorNode = vec4(c, 1) as N
    m.opacityNode = Fn(() => {
      alpha.lessThanEqual(0.001).discard()
      return alpha
    })() as N
    m.transparent = true
    m.depthWrite = false
    m.fog = false
    this.points = new Points(g, m)
    this.points.frustumCulled = false
    this.points.visible = false
    this.points.name = 'LidarScan'
    this.points.renderOrder = 40
  }

  setPersist(s: number): void {
    this.persistS = Math.min(LIDAR_SCAN.persistMaxS, Math.max(LIDAR_SCAN.persistMinS, Number.isFinite(s) ? s : LIDAR_SCAN.persistS))
    this.uPersist.value = this.persistS
  }

  /**
   * one LidarScan8 message (bytes as delivered by the gateway) born at nowS; returns the points written (a frame larger
   * than half the ring is subsampled evenly)
   */
  ingest(bytes: ArrayBufferView, nowS: number): number {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    const h = this.hdr
    if (!decodeLidarScanHeader(dv, h) || h.n === 0) return 0
    const step = Math.max(1, Math.ceil(h.n / Math.max(1, this.cap >> 1)))
    const qx = h.q[0], qy = h.q[1], qz = h.q[2], qw = h.q[3]
    // rotation matrix of the unit quaternion (WORLD <- SENSOR)
    const xx = qx * qx, yy = qy * qy, zz = qz * qz, xy = qx * qy, xz = qx * qz, yz = qy * qz, wx = qw * qx, wy = qw * qy, wz = qw * qz
    const m00 = 1 - 2 * (yy + zz), m01 = 2 * (xy - wz), m02 = 2 * (xz + wy)
    const m10 = 2 * (xy + wz), m11 = 1 - 2 * (xx + zz), m12 = 2 * (yz - wx)
    const m20 = 2 * (xz - wy), m21 = 2 * (yz + wx), m22 = 1 - 2 * (xx + yy)
    const P = this.pos.array as Float32Array
    const B = this.birth.array as Float32Array
    const Rf = this.refl.array as Uint8Array
    const start = this.head
    let w = 0
    for (let i = 0; i < h.n; i += step) {
      const o = h.offset + i * LIDAR_SCAN.point
      const x = dv.getInt16(o, true) / 100
      const y = dv.getInt16(o + 2, true) / 100
      const z = dv.getInt16(o + 4, true) / 100
      const k = this.head
      P[3 * k] = h.pos[0] + m00 * x + m01 * y + m02 * z
      P[3 * k + 1] = h.pos[1] + m10 * x + m11 * y + m12 * z
      P[3 * k + 2] = h.pos[2] + m20 * x + m21 * y + m22 * z
      B[k] = nowS
      Rf[k] = dv.getUint8(o + 6)
      this.head = (k + 1) % this.cap
      w++
    }
    this.used = Math.min(this.cap, this.used + w)
    this.newest = nowS
    this.frames++
    this.written += w
    // upload the written span (one or two ranges when it wraps)
    for (const a of [this.pos, this.birth, this.refl]) {
      a.clearUpdateRanges()
      const isz = a.itemSize
      if (start + w <= this.cap) a.addUpdateRange(start * isz, w * isz)
      else {
        a.addUpdateRange(start * isz, (this.cap - start) * isz)
        a.addUpdateRange(0, (start + w - this.cap) * isz)
      }
      a.needsUpdate = true
    }
    return w
  }

  /** per frame: fade time; drawn while the newest frame is younger than the persistence */
  update(nowS: number): void {
    this.uNow.value = nowS
    const alive = this.used > 0 && nowS - this.newest < this.persistS
    this.points.geometry.setDrawRange(0, alive ? this.used : 0)
    this.points.visible = alive
  }

  clear(): void {
    this.used = 0
    this.head = 0
    this.newest = Number.NEGATIVE_INFINITY
    this.points.visible = false
    this.points.geometry.setDrawRange(0, 0)
  }

  /** world points of ring slot k (tests) */
  pointAt(k: number, out: Float64Array): Float64Array {
    const P = this.pos.array as Float32Array
    out[0] = P[3 * k]
    out[1] = P[3 * k + 1]
    out[2] = P[3 * k + 2]
    return out
  }

  drawCount(): number {
    return this.points.visible ? 1 : 0
  }

  dispose(): void {
    this.points.geometry.dispose()
    ;(this.points.material as PointsNodeMaterial).dispose()
  }
}
