// DTM access for wind (terrain-following z_agl) on CPU and GPU (M07-FR-033; M07 §6.3.5; 16 §6.2). Owner: M07.
// CPU and TSL use the same cell-centre bilinear rule clamped at the border (M04 grids.Grid.bilinear); the GPU reads four
// texels with textureLoad (R32F linear filtering is not core WebGL2). The raster is M05's DtmSampler (services.dtm of the
// point-cloud layer: `texture` holds dtm - ground.zM, row 0 south); until it is loaded both sides use ground.zM (flat).
import { DataTexture, FloatType, NearestFilter, RedFormat, Vector2, type Texture } from 'three'
import { clamp, float, floor, int, ivec2, max, min, texture, textureLoad, uniform } from 'three/tsl'

type N = any // TSL nodes

/** the shape of M05's DtmSampler this module reads (duck-typed through viewport services) */
export interface DtmSource {
  loaded: boolean
  groundZ: number
  texture: DataTexture | null
  readonly grid: { width: number; height: number; cellM: number; originX: number; originY: number }
  sample(x: number, y: number): number
}

export class EnvTerrain {
  src: DtmSource | null = null
  groundZ = 0
  /** a 1 x 1 zero texture until the DTM is loaded (values are relative to groundZ) */
  private readonly flat: DataTexture
  /** base texture node shared by the environment materials (value swapped by sync(), no recompile) */
  readonly texNode: N
  readonly origin = new Vector2()
  readonly size = new Vector2(1, 1)
  cell = 1
  private readonly p = new Float32Array(5)

  constructor() {
    this.flat = new DataTexture(new Float32Array(1), 1, 1, RedFormat, FloatType)
    this.flat.minFilter = NearestFilter
    this.flat.magFilter = NearestFilter
    this.flat.needsUpdate = true
    this.texNode = texture(this.flat as Texture)
  }

  /** once per frame: follow the DTM source (loaded later, reloaded on a world switch) */
  sync(): void {
    if (this.src && this.src.loaded) this.groundZ = this.src.groundZ
    const t = this.texture()
    if (this.texNode.value !== t) this.texNode.value = t
    this.params(this.p)
    this.origin.set(this.p[0], this.p[1])
    this.cell = this.p[2]
    this.size.set(this.p[3], this.p[4])
  }

  get ready(): boolean {
    return !!this.src && this.src.loaded && !!this.src.texture
  }

  /** terrain height (m, world ENU) */
  sample(x: number, y: number): number {
    const s = this.src
    return s && s.loaded ? s.sample(x, y) : this.groundZ
  }

  texture(): DataTexture {
    const s = this.src
    return s && s.loaded && s.texture ? s.texture : this.flat
  }

  /** grid parameters for the TSL sampler: [originX, originY, cell, width, height] */
  params(out: Float32Array): Float32Array {
    const s = this.src
    if (s && s.loaded && s.texture) {
      const g = s.grid
      out[0] = g.originX
      out[1] = g.originY
      out[2] = g.cellM
      out[3] = g.width
      out[4] = g.height
    } else {
      out[0] = 0
      out[1] = 0
      out[2] = 1
      out[3] = 1
      out[4] = 1
    }
    return out
  }
}

/** per-material TSL uniforms of the DTM sampler (never shared between materials, M06 §6.3) */
export interface DtmNodes { tex: N; origin: N; cell: N; size: N; groundZ: N }

export function makeDtmNodes(t: EnvTerrain): DtmNodes {
  return {
    tex: t.texNode, origin: uniform(t.origin), size: uniform(t.size),
    cell: (uniform(1) as N).onRenderUpdate(() => t.cell), groundZ: (uniform(0) as N).onRenderUpdate(() => t.groundZ),
  }
}

/** terrain height at ENU xy (world z): four textureLoad taps, cell-centre bilinear, clamped (same as the CPU) */
export function dtmHeight(n: DtmNodes, xy: N, texNode: N): N {
  const w: N = n.size.x
  const h: N = n.size.y
  const gx: N = clamp(xy.x.sub(n.origin.x).div(n.cell).sub(0.5), float(0), max(w.sub(1), float(0)))
  const gy: N = clamp(xy.y.sub(n.origin.y).div(n.cell).sub(0.5), float(0), max(h.sub(1), float(0)))
  const c0: N = min(floor(gx), max(w.sub(2), float(0)))
  const r0: N = min(floor(gy), max(h.sub(2), float(0)))
  const tx: N = gx.sub(c0)
  const ty: N = gy.sub(r0)
  const dc: N = min(w.sub(1), float(1))
  const dr: N = min(h.sub(1), float(1))
  const ic: N = int(c0)
  const ir: N = int(r0)
  const idc: N = int(dc)
  const idr: N = int(dr)
  const v00: N = textureLoad(texNode, ivec2(ic, ir)).x
  const v01: N = textureLoad(texNode, ivec2(ic.add(idc), ir)).x
  const v10: N = textureLoad(texNode, ivec2(ic, ir.add(idr))).x
  const v11: N = textureLoad(texNode, ivec2(ic.add(idc), ir.add(idr))).x
  const v: N = v00.mul(float(1).sub(tx)).add(v01.mul(tx)).mul(float(1).sub(ty)).add(v10.mul(float(1).sub(tx)).add(v11.mul(tx)).mul(ty))
  return v.add(n.groundZ)
}
