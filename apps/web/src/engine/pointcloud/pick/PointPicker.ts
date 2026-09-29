// Point picking, M05 side (D1-ext; M05 §6.9, M05-FR-047, FR-048; M06-FR-063, FR-064). Owner: M05.
// M06's Picker orchestrates: prepare() builds the pick sub-table from this frame's DrawTable entries whose tight box meets
// the pick ray (so the ID pass draws only a few thousand points, never the whole budget) and keeps a snapshot of their
// prefixes; M06 renders `object` (channel CH_PICK, ID material, id = vid + 1 in the RGB of RGBA8) into its 5 x 5 raster-px pick
// target on the next frame and reads it back asynchronously; decode() takes the non-zero pixel nearest to the centre,
// finds the node and the node-local index in the snapshot and decodes the point from the CPU cache (resident nodes are
// always cached, M05-FR-021: no request). One ticket at a time: a new prepare() voids the old one; after 2 s a decode
// returns null (411 PC_PICK_TIMEOUT); no hit returns null (410 PC_PICK_MISS).
import { BufferGeometry, DataTexture, Matrix4, NearestFilter, Points, RGBAIntegerFormat, Sphere, UnsignedIntType, Vector3 } from 'three'
import { texture, uniform } from 'three/tsl'
import type { NodeStore } from '../core/NodeStore'
import type { DtmSampler } from '../io/dtm'
import { decodeOct16, decodePoint } from '../io/q16'
import { PC } from '../params'
import type { PointPick } from '../types'
import type { N } from '../render/fetchNode'

export interface PickTicket {
  id: number
  n: number
  points: number
  node: Int32Array
  prefix: Int32Array
  createdAt: number
  rasterX: number
  rasterY: number
  rayDir: Float64Array
}

export interface PickSource {
  store(): NodeStore | null
  /** current DrawTable words and entry count */
  drawTable(): { data: Uint32Array; k: number }
  packed(i: number): Uint32Array | null
  dtm: DtmSampler
  /** layer -> world (ENU) matrix, column-major, and its inverse */
  layerMatrix(): { m: Matrix4; inv: Matrix4 }
  className(cls: number): string
  now(): number
}

export class PointPicker {
  readonly object: Points
  readonly data: Uint32Array
  readonly tex: DataTexture
  readonly texNode: N
  readonly numDraws: N = uniform(0)
  private ticket: PickTicket | null = null
  private seq = 0
  private readonly o = new Vector3()
  private readonly d = new Vector3()
  private readonly tmp = new Float64Array(3)
  private readonly nrm = new Float64Array(3)
  misses = 0
  timeouts = 0

  constructor(private readonly src: PickSource) {
    this.data = new Uint32Array(PC.drawTableWidth * PC.drawTableRows * 4)
    this.tex = new DataTexture(this.data, PC.drawTableWidth, PC.drawTableRows, RGBAIntegerFormat, UnsignedIntType)
    this.tex.minFilter = NearestFilter
    this.tex.magFilter = NearestFilter
    this.tex.generateMipmaps = false
    this.tex.needsUpdate = true
    this.texNode = texture(this.tex)
    const g = new BufferGeometry()
    g.setDrawRange(0, 0)
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    this.object = new Points(g)
    this.object.name = 'PointCloudPick'
    this.object.frustumCulled = false
    this.object.layers.set(2) // CH_PICK
    this.object.visible = false
    this.object.raycast = () => {}
  }

  /** build the pick sub-table; null when the ray meets no drawn node (M06 then falls back to the ground pick) */
  prepare(rasterX: number, rasterY: number, rayOriginEnu: ArrayLike<number>, rayDirEnu: ArrayLike<number>): PickTicket | null {
    this.ticket = null
    const t = this.src.store()
    if (!t) return null
    const { inv } = this.src.layerMatrix()
    this.o.set(rayOriginEnu[0], rayOriginEnu[1], rayOriginEnu[2]).applyMatrix4(inv)
    this.d.set(rayDirEnu[0], rayDirEnu[1], rayDirEnu[2]).transformDirection(inv)
    const { data, k } = this.src.drawTable()
    const out = this.data
    let j = 0
    let prefix = 0
    const node = new Int32Array(k)
    const pre = new Int32Array(k)
    for (let e = 0; e < k; e++) {
      const i = data[4 * e + 3] & 0xffffff
      if (!rayHitsBox(this.o, this.d, t.tightMin, t.tightMax, i)) continue
      const cnt = data[4 * e + 2] & 0xffffff
      out[4 * j] = prefix
      out[4 * j + 1] = data[4 * e + 1]
      out[4 * j + 2] = data[4 * e + 2]
      out[4 * j + 3] = data[4 * e + 3]
      node[j] = i
      pre[j] = prefix
      prefix += cnt
      j++
    }
    if (j === 0) {
      this.misses++
      this.object.visible = false
      return null
    }
    this.tex.clearUpdateRanges()
    for (let r = 0; r * PC.drawTableWidth < j; r++) this.tex.addUpdateRange(r * PC.drawTableWidth * 4, Math.min(j - r * PC.drawTableWidth, PC.drawTableWidth) * 4)
    this.tex.needsUpdate = true
    this.numDraws.value = j
    this.object.geometry.setDrawRange(0, prefix)
    this.object.visible = true
    this.ticket = { id: ++this.seq, n: j, points: prefix, node: node.subarray(0, j), prefix: pre.subarray(0, j), createdAt: this.src.now(), rasterX, rasterY,
      rayDir: Float64Array.from([rayDirEnu[0], rayDirEnu[1], rayDirEnu[2]]) }
    return this.ticket
  }

  /** after M06 rendered the pick pass: hide the object again */
  end(): void {
    this.object.visible = false
  }

  /** pixels: 5 x 5 x RGBA8, rows bottom-up (M06 readPixels); resolves the picked point or null */
  async decode(ticket: PickTicket, pixels: Uint8Array, w = PC.pickWindow, h = PC.pickWindow): Promise<PointPick | null> {
    this.end()
    if (ticket !== this.ticket) return null
    if (this.src.now() - ticket.createdAt > PC.pickTimeoutMs) {
      this.timeouts++
      return null
    }
    const cx = (w - 1) / 2
    const cy = (h - 1) / 2
    let best = -1
    let bestD = Number.POSITIVE_INFINITY
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const o = 4 * (y * w + x)
        const id = pixels[o] | (pixels[o + 1] << 8) | (pixels[o + 2] << 16) // 24-bit id in RGB (alpha is forced to 1)
        if (id === 0) continue
        const dd = (x - cx) ** 2 + (y - cy) ** 2
        if (dd < bestD) {
          bestD = dd
          best = id >>> 0
        }
      }
    }
    if (best <= 0) {
      this.misses++
      return null
    }
    return this.pointOf(ticket, best - 1)
  }

  /** node and node-local index of pick vertex vid, decoded from the CPU cache */
  pointOf(ticket: PickTicket, vid: number): PointPick | null {
    const t = this.src.store()
    if (!t || vid < 0 || vid >= ticket.points) return null
    let lo = 0
    let hi = ticket.n
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1
      if (ticket.prefix[mid] <= vid) lo = mid
      else hi = mid
    }
    const i = ticket.node[lo]
    const local = vid - ticket.prefix[lo]
    const packed = this.src.packed(i)
    if (!packed || local >= t.numPoints[i]) return null
    decodePoint(packed, local, t.cubeMin, 3 * i, t.cubeSize[i], this.tmp)
    const { m } = this.src.layerMatrix()
    const p = new Vector3(this.tmp[0], this.tmp[1], this.tmp[2]).applyMatrix4(m)
    const pos = Float64Array.from([p.x, p.y, p.z])
    const w1 = packed[4 * local + 1]
    const cls = packed[4 * local + 2] >>> 24
    let normal: Float64Array | null = null
    if (decodeOct16(w1 >>> 16, this.nrm)) {
      const r = ticket.rayDir
      const s = this.nrm[0] * r[0] + this.nrm[1] * r[1] + this.nrm[2] * r[2] > 0 ? -1 : 1 // face the viewer
      normal = Float64Array.from([s * this.nrm[0], s * this.nrm[1], s * this.nrm[2]])
    }
    const dtm = this.src.dtm
    return {
      posEnuM: pos, classIdx: cls, className: this.src.className(cls), normal, hagM: dtm.loaded ? pos[2] - dtm.sample(pos[0], pos[1]) : null, nodeId: i,
      nodeName: t.names[i], local, spacingM: t.spacing[i],
    }
  }

  dispose(): void {
    this.tex.dispose()
    this.object.geometry.dispose()
  }
}

/** ray (origin o, direction d) against node i's tight box, slab test, t >= 0 */
export function rayHitsBox(o: Vector3, d: Vector3, tmin: Float64Array, tmax: Float64Array, i: number): boolean {
  let t0 = 0
  let t1 = Number.POSITIVE_INFINITY
  const b = 3 * i
  const ro = [o.x, o.y, o.z]
  const rd = [d.x, d.y, d.z]
  for (let a = 0; a < 3; a++) {
    const lo = tmin[b + a]
    const hi = tmax[b + a]
    if (Math.abs(rd[a]) < 1e-12) {
      if (ro[a] < lo || ro[a] > hi) return false
      continue
    }
    let ta = (lo - ro[a]) / rd[a]
    let tb = (hi - ro[a]) / rd[a]
    if (ta > tb) {
      const s = ta
      ta = tb
      tb = s
    }
    if (ta > t0) t0 = ta
    if (tb < t1) t1 = tb
    if (t0 > t1) return false
  }
  return true
}
