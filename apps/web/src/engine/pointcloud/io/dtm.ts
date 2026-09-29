// DTM sampler (M05-FR-060; AWR-16 §6.2, §6.3; M06-FR-056). Owner: M05.
// Loaded after the first screen (never on the TTFP path): the grid sidecar geometry/terrain/dtm_10m.json and its f32
// raster. sample(x, y) is the cell-centre bilinear interpolation of M04 grids.Grid.bilinear (clamped at the border,
// float64), used by M06 for the camera ground clamp and by picking for HAG; before the load it returns
// coordinate.ground.zM. The HAG colour mode reads the same raster from an R32F texture (dtm - ground.zM) with the same
// bilinear rule in the vertex shader (4 texel loads), so CPU and GPU agree exactly.
import { DataTexture, FloatType, NearestFilter, RedFormat } from 'three'
import type { GridSidecar } from '../types'

export class DtmSampler {
  private data: Float32Array | null = null
  private w = 0
  private h = 0
  private x0 = 0
  private y0 = 0
  private cell = 1
  groundZ = 0
  texture: DataTexture | null = null
  loaded = false
  bytes = 0

  /** install a raster (row 0 south) */
  set(sc: Pick<GridSidecar, 'width' | 'height' | 'cellM' | 'originXY'>, data: Float32Array, groundZ: number): void {
    if (data.length !== sc.width * sc.height) throw new Error(`dtm raster has ${data.length} cells, sidecar says ${sc.width} x ${sc.height}`)
    this.data = data
    this.w = sc.width
    this.h = sc.height
    this.x0 = sc.originXY[0]
    this.y0 = sc.originXY[1]
    this.cell = sc.cellM
    this.groundZ = groundZ
    this.loaded = true
    const rel = new Float32Array(data.length)
    for (let i = 0; i < data.length; i++) rel[i] = data[i] - groundZ
    this.texture?.dispose()
    const t = new DataTexture(rel, this.w, this.h, RedFormat, FloatType)
    t.minFilter = NearestFilter
    t.magFilter = NearestFilter
    t.generateMipmaps = false
    t.flipY = false
    t.needsUpdate = true
    this.texture = t
  }

  reset(groundZ = 0): void {
    this.data = null
    this.loaded = false
    this.groundZ = groundZ
  }

  get grid(): { width: number; height: number; cellM: number; originX: number; originY: number } {
    return { width: this.w, height: this.h, cellM: this.cell, originX: this.x0, originY: this.y0 }
  }

  /** terrain height (m, world ENU) at (x, y): cell-centre bilinear, clamped outside the grid; ground.zM before the load */
  sample(x: number, y: number): number {
    const a = this.data
    if (!a) return this.groundZ
    const w = this.w
    const h = this.h
    const gx = Math.min(Math.max((x - this.x0) / this.cell - 0.5, 0), Math.max(w - 1, 0))
    const gy = Math.min(Math.max((y - this.y0) / this.cell - 0.5, 0), Math.max(h - 1, 0))
    const c0 = Math.min(Math.floor(gx), Math.max(w - 2, 0))
    const r0 = Math.min(Math.floor(gy), Math.max(h - 2, 0))
    const tx = gx - c0
    const ty = gy - r0
    const i = r0 * w + c0
    const dc = w >= 2 ? 1 : 0
    const dr = h >= 2 ? w : 0
    const v00 = a[i]
    const v01 = a[i + dc]
    const v10 = a[i + dr]
    const v11 = a[i + dr + dc]
    return (v00 * (1 - tx) + v01 * tx) * (1 - ty) + (v10 * (1 - tx) + v11 * tx) * ty
  }

  /**
   * Load sidecar + raster: base is the world directory URL, href the sidecar path (coordinate.ground.dtm.href),
   * cv the contentVersion (every request carries ?v=). get performs the GET (counted by the fetcher).
   */
  async load(base: string, href: string, cv: string, groundZ: number, get: (url: string) => Promise<Response>): Promise<void> {
    const v = `?v=${encodeURIComponent(cv)}`
    const scUrl = new URL(href, base)
    const r = await get(`${scUrl.href}${v}`)
    if (!r.ok) throw new Error(`dtm sidecar ${r.status}`)
    const sc = (await r.json()) as GridSidecar
    if (sc.dtype !== 'float32' || sc.rowOrder !== 'south-to-north') throw new Error(`dtm sidecar: dtype ${sc.dtype}, rowOrder ${sc.rowOrder}`)
    const raw = await get(`${new URL(sc.href, scUrl).href}${v}`)
    if (!raw.ok) throw new Error(`dtm raster ${raw.status}`)
    const buf = await raw.arrayBuffer()
    this.bytes = buf.byteLength
    this.set(sc, new Float32Array(buf), groundZ)
  }

  dispose(): void {
    this.texture?.dispose()
    this.texture = null
    this.data = null
    this.loaded = false
  }
}
