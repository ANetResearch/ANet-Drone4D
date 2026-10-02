// DrawTable and NodeTable (M05 §6.2.3, §6.7.1, M05-FR-028, FR-029, FR-031). Owner: M05.
// buildDrawTable is pure over typed arrays (unit tests call it without a GPU): one entry per selected and resident node
// in pop order, x = prefixStart, y = poolBase, z = cnt | childDrawnMask << 24, w = nodeIdx | round(fade x 255) << 24.
// It also writes the hysteresis mark t.drawnFrame = sel.frame for every selected node that is resident (and for nodes
// without own points, which count as resident and faded in). childDrawnMask counts only children drawn this frame
// whose fade reached 1, so a parent octant shrinks one level only after the child finished fading in.
import { DataTexture, FloatType, NearestFilter, RGBAFormat, RGBAIntegerFormat, RedIntegerFormat, UnsignedIntType } from 'three'
import type { NodeStore } from '../core/NodeStore'
import type { Selection } from '../core/Selector'
import { PC } from '../params'

export interface DrawBuild {
  /** entries written */
  k: number
  /** points drawn (sum of cnt over the entries) */
  drawn: number
  /** selected nodes that are resident (or have no own points) */
  residentSel: number
}
export const newDrawBuild = (): DrawBuild => ({ k: 0, drawn: 0, residentSel: 0 })

export function buildDrawTable(sel: Selection, t: NodeStore, nowMs: number, fadeMs: number, ease: (x: number) => number, reduced: boolean,
  out: Uint32Array, res: DrawBuild): DrawBuild {
  const frame = sel.frame
  let prefix = 0
  let k = 0
  let rs = 0
  for (let j = 0; j < sel.n; j++) {
    const i = sel.idx[j]
    if (t.numPoints[i] === 0) {
      t.drawnFrame[i] = frame
      t.fade[i] = 1
      rs++
      continue
    }
    if (t.poolBase[i] < 0) continue // not resident: not drawn, the parent octant keeps its point size
    rs++
    let f = 1
    if (!reduced) {
      const x = (nowMs - t.fadeStart[i]) / fadeMs
      f = x >= 1 ? 1 : x <= 0 ? 0 : ease(x)
    }
    t.fade[i] = f
    t.drawnFrame[i] = frame
    const o = 4 * k
    out[o] = prefix
    out[o + 1] = t.poolBase[i]
    out[o + 2] = sel.cnt[j]
    out[o + 3] = i
    prefix += sel.cnt[j]
    k++
  }
  for (let e = 0; e < k; e++) {
    const o = 4 * e
    const i = out[o + 3]
    let m = 0
    for (let c = 0; c < 8; c++) {
      const ch = t.children[8 * i + c]
      if (ch >= 0 && t.drawnFrame[ch] === frame && t.fade[ch] >= 1) m |= 1 << c
    }
    out[o + 2] = (out[o + 2] | (m << 24)) >>> 0
    out[o + 3] = (i | (Math.round(t.fade[i] * 255) << 24)) >>> 0
  }
  res.k = k
  res.drawn = prefix
  res.residentSel = rs
  return res
}

/** vertices per DrawTable block (block index, FX2-R2) */
export const DRAW_BLOCK = 1 << PC.drawIndexBlockLog2

/**
 * Block index of a DrawTable (pure; FX2-R2): out[b] = the entry containing vertex b·DRAW_BLOCK for b < nb =
 * ceil(drawn / DRAW_BLOCK), out[nb] = the last entry. The entry of vertex v lies in [out[v >> log2], out[(v >> log2) + 1]]
 * (largest prefixStart <= v, zero-length entries resolve to the later one as in the binary search). Returns nb + 1, the
 * number of words written.
 */
export function buildBlockIndex(entries: Uint32Array, k: number, drawn: number, out: Uint32Array): number {
  const nb = Math.min(Math.ceil(drawn / DRAW_BLOCK), out.length - 1)
  let e = 0
  for (let b = 0; b < nb; b++) {
    const v = b * DRAW_BLOCK
    while (e + 1 < k && entries[4 * (e + 1)] <= v) e++
    out[b] = e
  }
  out[nb] = k > 0 ? k - 1 : 0
  return nb + 1
}

function intTexture(data: Uint32Array, w: number, h: number, format: typeof RGBAIntegerFormat | typeof RedIntegerFormat = RGBAIntegerFormat): DataTexture {
  const tex = new DataTexture(data, w, h, format, UnsignedIntType)
  tex.minFilter = NearestFilter
  tex.magFilter = NearestFilter
  tex.generateMipmaps = false
  tex.flipY = false
  return tex
}

/** NodeTable texel rows for N nodes (two texels per node, 1024 wide) */
export const nodeTableRows = (N: number): number => Math.max(1, Math.ceil((2 * N) / PC.nodeTableWidth))

/**
 * GPU side of the tables: DrawTable RGBA32UI 1024 x 4 rewritten every frame (only the rows in use are uploaded, one
 * addUpdateRange per row, M05-FR-028), its block index R32UI 1024 wide (buildBlockIndex, rows for maxPoints, uploaded
 * the same way) and NodeTable RGBA32F, two texels per node, (min.xyz, cubeSize) and (spacing_L, level, leaf, 0) in the
 * layer frame (leaf = 1 without a child in the data, ADR-063), written once per world. No internalFormat on any
 * (g01 §0 item 10).
 */
export class DrawTables {
  readonly draw: DataTexture
  readonly drawData: Uint32Array
  readonly block: DataTexture
  readonly blockData: Uint32Array
  readonly node: DataTexture
  readonly nodeData: Float32Array
  readonly nodeCapacity: number
  readonly maxPoints: number

  /** maxPoints: the most points one frame can draw (the pool capacity) */
  constructor(nodeCapacity: number, maxPoints: number) {
    this.drawData = new Uint32Array(PC.drawTableWidth * PC.drawTableRows * 4)
    this.draw = intTexture(this.drawData, PC.drawTableWidth, PC.drawTableRows)
    this.draw.needsUpdate = true
    this.maxPoints = maxPoints
    const blockRows = Math.max(1, Math.ceil((Math.ceil(maxPoints / DRAW_BLOCK) + 2) / PC.drawTableWidth))
    this.blockData = new Uint32Array(PC.drawTableWidth * blockRows)
    this.block = intTexture(this.blockData, PC.drawTableWidth, blockRows, RedIntegerFormat)
    this.block.needsUpdate = true
    this.nodeCapacity = nodeCapacity
    const rows = nodeTableRows(nodeCapacity)
    this.nodeData = new Float32Array(PC.nodeTableWidth * rows * 4)
    this.node = new DataTexture(this.nodeData, PC.nodeTableWidth, rows, RGBAFormat, FloatType)
    this.node.minFilter = NearestFilter
    this.node.magFilter = NearestFilter
    this.node.generateMipmaps = false
    this.node.needsUpdate = true
  }

  /** write the node table of a world (layer frame, float32) */
  setNodes(t: NodeStore): void {
    const d = this.nodeData
    d.fill(0)
    const n = Math.min(t.N, this.nodeCapacity)
    for (let i = 0; i < n; i++) {
      const o = 8 * i
      d[o] = t.cubeMin[3 * i]
      d[o + 1] = t.cubeMin[3 * i + 1]
      d[o + 2] = t.cubeMin[3 * i + 2]
      d[o + 3] = t.cubeSize[i]
      d[o + 4] = t.spacing[i]
      d[o + 5] = t.level[i]
      // leaf flag (no child in the data): leaf points keep the rung maxPx in dense frames (ADR-063)
      let leaf = 1
      for (let c = 0; c < 8; c++) if (t.children[8 * i + c] >= 0) leaf = 0
      d[o + 6] = leaf
    }
    this.node.needsUpdate = true
  }

  /** upload only the rows touched by k entries (addUpdateRange in Uint32 elements, never across a row) */
  commit(k: number): void {
    const perRow = PC.drawTableWidth * 4
    const rows = Math.ceil(k / PC.drawTableWidth)
    this.draw.clearUpdateRanges()
    for (let r = 0; r < rows; r++) {
      const n = Math.min(k - r * PC.drawTableWidth, PC.drawTableWidth)
      if (n > 0) this.draw.addUpdateRange(r * perRow, n * 4)
    }
    if (k > 0) this.draw.needsUpdate = true
  }

  /**
   * upload the first n words of the block index (rows touched only, as commit). three r186 converts update ranges to
   * texels with a fixed stride of 4 components ("only RGBA supported", WebGLTextures.updateTexture) while the
   * unpack skip is counted in texels of the actual format, so the ranges of this one-component texture are given x 4
   */
  commitBlocks(n: number): void {
    const W = PC.drawTableWidth
    const rows = Math.ceil(n / W)
    this.block.clearUpdateRanges()
    for (let r = 0; r < rows; r++) {
      const m = Math.min(n - r * W, W)
      if (m > 0) this.block.addUpdateRange(4 * r * W, 4 * m)
    }
    if (n > 0) this.block.needsUpdate = true
  }

  dispose(): void {
    this.draw.dispose()
    this.block.dispose()
    this.node.dispose()
  }
}
