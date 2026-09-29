// PointPool (ADR-010; M05 §6.2.4, §6.6.2, §6.6.3, M05-FR-022, FR-023): one RGBA32UI texel per point, 4096 texels per
// row, allocated on the GPU without a CPU mirror. Owner: M05.
// Allocation: DataTexture(null) with source.dataReady = false and one initTexture(): three runs texStorage2D only
// (WebGLTextures.setTexture2D / DataTexture branch); needsUpdate is never set again afterwards.
// Upload: a node's packed words are written into a staging DataTexture at the destination column offset and copied with
// copyTextureToTexture in at most three rectangles (first partial row, full rows, last partial row). The staging texture
// is never bound to a material and never initialised, so three takes the texSubImage2D(image.data) path with
// UNPACK_ROW_LENGTH = 4096 (three r186 WebGLRenderer.copyTextureToTexture, properties.has(src) === false).
import { Box2, DataTexture, NearestFilter, RGBAIntegerFormat, UnsignedIntType, Vector2 } from 'three'
import type { TextureOps } from '../../loop'
import { PC } from '../params'

const W = PC.poolWidth

function intTexture(data: Uint32Array | null, w: number, h: number): DataTexture {
  const t = new DataTexture(data, w, h, RGBAIntegerFormat, UnsignedIntType)
  t.minFilter = NearestFilter
  t.magFilter = NearestFilter
  t.generateMipmaps = false
  t.flipY = false
  return t
}

export class PointPool {
  readonly tex: DataTexture
  readonly rows: number
  staging: DataTexture
  stagingData: Uint32Array
  stagingRows: number
  private readonly box = new Box2()
  private readonly dst = new Vector2()
  /** copy rectangles issued (tests) */
  copies = 0

  constructor(rows: number, stagingRows: number, private readonly ops: TextureOps) {
    this.rows = rows
    this.tex = intTexture(null, W, rows)
    this.tex.source.dataReady = false
    this.tex.needsUpdate = true
    ops.initTexture(this.tex)
    this.stagingRows = Math.max(2, stagingRows)
    this.stagingData = new Uint32Array(W * this.stagingRows * 4)
    this.staging = intTexture(this.stagingData, W, this.stagingRows)
  }

  get capacity(): number {
    return W * this.rows
  }

  /** staging rows for nodes of up to maxPts points: ceil(maxPts / 4096) + 1 (a node may start mid-row) */
  ensureStaging(maxPts: number): void {
    const rows = Math.max(2, Math.ceil(maxPts / W) + 1)
    if (rows <= this.stagingRows) return
    this.staging.dispose()
    this.stagingRows = rows
    this.stagingData = new Uint32Array(W * rows * 4)
    this.staging = intTexture(this.stagingData, W, rows)
  }

  /** copy n packed points (4 words each) to texel address base */
  upload(base: number, packed: Uint32Array, n: number): void {
    if (n <= 0) return
    const row = Math.floor(base / W)
    const col = base % W
    this.stagingData.set(packed.subarray(0, 4 * n), 4 * col)
    const w1 = Math.min(W - col, n)
    this.copy(col, 0, w1, 1, col, row)
    let rest = n - w1
    if (rest <= 0) return
    const full = Math.floor(rest / W)
    if (full > 0) this.copy(0, 1, W, full, 0, row + 1)
    rest -= full * W
    if (rest > 0) this.copy(0, 1 + full, rest, 1, 0, row + 1 + full)
  }

  private copy(sx: number, sy: number, w: number, h: number, dx: number, dy: number): void {
    this.box.min.set(sx, sy)
    this.box.max.set(sx + w, sy + h)
    this.dst.set(dx, dy)
    this.ops.copyTextureToTexture(this.staging, this.tex, this.box, this.dst)
    this.copies++
  }

  dispose(): void {
    this.tex.dispose()
    this.staging.dispose()
  }
}

/** 1-texel stand-ins used by warm-up variants and before a world is open */
export function dummyIntTexture(): DataTexture {
  const t = intTexture(new Uint32Array(4), 1, 1)
  t.needsUpdate = true
  return t
}
