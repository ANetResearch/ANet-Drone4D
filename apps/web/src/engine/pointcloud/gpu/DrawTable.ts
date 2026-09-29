// DrawTable and NodeTable (M05 §6.2.3, §6.7.1, M05-FR-028, FR-029, FR-031). Owner: M05.
// buildDrawTable is pure over typed arrays (unit tests call it without a GPU): one entry per selected and resident node
// in pop order, x = prefixStart, y = poolBase, z = cnt | childDrawnMask << 24, w = nodeIdx | round(fade x 255) << 24.
// It also writes the hysteresis mark t.drawnFrame = sel.frame for every selected node that is resident (and for nodes
// without own points, which count as resident and faded in). childDrawnMask counts only children drawn this frame
// whose fade reached 1, so a parent octant shrinks one level only after the child finished fading in.
import { DataTexture, FloatType, NearestFilter, RGBAFormat, RGBAIntegerFormat, UnsignedIntType } from 'three'
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

function intTexture(data: Uint32Array, w: number, h: number): DataTexture {
  const tex = new DataTexture(data, w, h, RGBAIntegerFormat, UnsignedIntType)
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
 * addUpdateRange per row, M05-FR-028) and NodeTable RGBA32F, two texels per node, (min.xyz, cubeSize) and
 * (spacing_L, level, 0, 0) in the layer frame, written once per world. No internalFormat on either (g01 §0 item 10).
 */
export class DrawTables {
  readonly draw: DataTexture
  readonly drawData: Uint32Array
  readonly node: DataTexture
  readonly nodeData: Float32Array
  readonly nodeCapacity: number

  constructor(nodeCapacity: number) {
    this.drawData = new Uint32Array(PC.drawTableWidth * PC.drawTableRows * 4)
    this.draw = intTexture(this.drawData, PC.drawTableWidth, PC.drawTableRows)
    this.draw.needsUpdate = true
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

  dispose(): void {
    this.draw.dispose()
    this.node.dispose()
  }
}
