// Label declutter grid (M06 §6.13, FR-066, AC-047; r14 §3.8). Owner: M06.
// Candidates are sorted by priority (selected 7 > red entity 6 > critical 5 > warning 4 > hover 3 > GoTo readout 2 >
// other 1; ties by distance) and placed on a screen grid of 96 x 28 CSS px cells: a label takes 2 columns x 1 row
// around its anchor; an occupied cell rejects the label. At most `cap` labels are kept (Tier S 16, B/A 48; PerfGovernor
// step 3: 8 and 4, or 24 and 12). No scene raycast occlusion. Zero allocation after construction.
export const DECLUTTER = { cellW: 96, cellH: 28, spanCols: 2, capS: 16, capBA: 48, levelsS: [16, 8, 4], levelsBA: [48, 24, 12] } as const

export class Declutter {
  private grid = new Uint8Array(0)
  private cols = 0
  private rows = 0
  private order: Int32Array
  private key: Float64Array

  constructor(readonly maxCandidates: number) {
    this.order = new Int32Array(maxCandidates)
    this.key = new Float64Array(maxCandidates)
  }

  /**
   * place n candidates (screen x, y of the label centre in CSS px, priority, distance); writes the accepted candidate
   * indices into out and returns their count (<= cap)
   */
  place(n: number, sx: Float32Array, sy: Float32Array, prio: Uint8Array, dist: Float32Array, w: number, h: number, cap: number, out: Int32Array): number {
    const cols = Math.max(1, Math.ceil(w / DECLUTTER.cellW) + 1)
    const rows = Math.max(1, Math.ceil(h / DECLUTTER.cellH) + 1)
    if (cols * rows > this.grid.length) this.grid = new Uint8Array(cols * rows * 2)
    this.cols = cols
    this.rows = rows
    this.grid.fill(0, 0, cols * rows)
    const m = Math.min(n, this.maxCandidates)
    // sort key: priority desc, distance asc (insertion sort on a small list)
    for (let i = 0; i < m; i++) {
      this.order[i] = i
      this.key[i] = prio[i] * 1e7 - Math.min(dist[i], 9.99e6)
    }
    for (let i = 1; i < m; i++) {
      const o = this.order[i]
      const k = this.key[o]
      let j = i - 1
      while (j >= 0 && this.key[this.order[j]] < k) {
        this.order[j + 1] = this.order[j]
        j--
      }
      this.order[j + 1] = o
    }
    let c = 0
    for (let i = 0; i < m && c < cap; i++) {
      const k = this.order[i]
      const c0 = Math.floor(sx[k] / DECLUTTER.cellW - 0.5)
      const r = Math.floor(sy[k] / DECLUTTER.cellH)
      if (r < 0 || r >= rows || c0 + DECLUTTER.spanCols <= 0 || c0 >= cols) continue
      let free = true
      for (let dc = 0; dc < DECLUTTER.spanCols; dc++) {
        const cc = c0 + dc
        if (cc >= 0 && cc < cols && this.grid[r * cols + cc] !== 0) free = false
      }
      if (!free) continue
      for (let dc = 0; dc < DECLUTTER.spanCols; dc++) {
        const cc = c0 + dc
        if (cc >= 0 && cc < cols) this.grid[r * cols + cc] = 1
      }
      out[c++] = k
    }
    return c
  }

  get gridSize(): { cols: number; rows: number } {
    return { cols: this.cols, rows: this.rows }
  }
}
