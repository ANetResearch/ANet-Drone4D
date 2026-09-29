// CPU trail history (M06-FR-042, FR-043; M06 §6.10; r15 §3.10). Owner: M06.
// Every present vehicle keeps a ring of 256 samples {x, y, z, t} (Float32, ENU m and sim seconds relative to the time
// block start, blocks <= 2 h; AWR-03 §5.2 item 3). A sample is appended when >= 0.5 s of sim time or >= 5 m of travel
// passed since the last one (15 m/s: ~85 s of history; hovering: 128 s). Preallocated for `maxDrones` (1000 x 256 x
// 16 B = 4 MB, NFR-011); vehicles map to rows through a 65536-entry table. Epoch changes clear everything, rflags.RESET
// clears one producer range, block rebases shift all times and bump `rebaseCount` so the GPU copies re-upload.
export const TRAIL = { samples: 256, minDtS: 0.5, minMoveM: 5, blockS: 7200, windowS: 120 } as const

export class TrailRing {
  readonly data: Float32Array
  readonly head: Int32Array
  readonly count: Int32Array
  /** total samples ever appended per row (monotonic, for GPU sync) */
  readonly seq: Float64Array
  readonly agentOfRow: Int32Array
  private readonly rowOf = new Int32Array(65536).fill(-1)
  private readonly free: Int32Array
  private freeN: number
  /** sim seconds of the current block start */
  blockStartS = Number.NaN
  rebaseCount = 0

  constructor(readonly maxDrones: number, readonly samples: number = TRAIL.samples) {
    this.data = new Float32Array(maxDrones * samples * 4)
    this.head = new Int32Array(maxDrones)
    this.count = new Int32Array(maxDrones)
    this.seq = new Float64Array(maxDrones)
    this.agentOfRow = new Int32Array(maxDrones).fill(-1)
    this.free = new Int32Array(maxDrones)
    for (let i = 0; i < maxDrones; i++) this.free[i] = maxDrones - 1 - i
    this.freeN = maxDrones
  }

  rowFor(agentNo: number): number {
    return this.rowOf[agentNo & 0xffff]
  }

  private alloc(agentNo: number): number {
    let r = this.rowOf[agentNo & 0xffff]
    if (r >= 0) return r
    if (this.freeN === 0) return -1
    r = this.free[--this.freeN]
    this.rowOf[agentNo & 0xffff] = r
    this.agentOfRow[r] = agentNo
    this.head[r] = 0
    this.count[r] = 0
    return r
  }

  /** time relative to the block (rebases when the block exceeds 2 h) */
  rel(tS: number): number {
    if (Number.isNaN(this.blockStartS)) this.blockStartS = tS
    if (tS - this.blockStartS > TRAIL.blockS) this.rebase(tS - TRAIL.windowS)
    return tS - this.blockStartS
  }

  private rebase(newStart: number): void {
    const d = newStart - this.blockStartS
    this.blockStartS = newStart
    const n = this.maxDrones * this.samples
    for (let i = 0; i < n; i++) this.data[4 * i + 3] -= d
    this.rebaseCount++
  }

  /** append when 0.5 s or 5 m passed; true when a sample was added */
  append(agentNo: number, x: number, y: number, z: number, tS: number): boolean {
    const r = this.alloc(agentNo)
    if (r < 0) return false
    const t = this.rel(tS)
    const c = this.count[r]
    const S = this.samples
    if (c > 0) {
      const lo = 4 * (r * S + ((this.head[r] - 1 + S) % S))
      const d = this.data
      const dt = t - d[lo + 3]
      if (dt < 0) {
        // time went backwards (seek or replay): restart this row
        this.count[r] = 0
        this.head[r] = 0
      } else if (dt < TRAIL.minDtS - 1e-4 && Math.hypot(x - d[lo], y - d[lo + 1], z - d[lo + 2]) < TRAIL.minMoveM) return false
    }
    const o = 4 * (r * S + this.head[r])
    this.data[o] = x
    this.data[o + 1] = y
    this.data[o + 2] = z
    this.data[o + 3] = t
    this.head[r] = (this.head[r] + 1) % S
    this.count[r] = Math.min(S, this.count[r] + 1)
    this.seq[r]++
    return true
  }

  /** j-th oldest sample of a row into out[0..3] (j < count) */
  sample(r: number, j: number, out: Float32Array | Float64Array, o = 0): void {
    const S = this.samples
    const k = (this.head[r] - this.count[r] + j + S) % S
    const i = 4 * (r * S + k)
    out[o] = this.data[i]
    out[o + 1] = this.data[i + 1]
    out[o + 2] = this.data[i + 2]
    out[o + 3] = this.data[i + 3]
  }

  /** forget one vehicle (removal, RESET of its producer) */
  clear(agentNo: number): void {
    const r = this.rowOf[agentNo & 0xffff]
    if (r < 0) return
    this.count[r] = 0
    this.head[r] = 0
    this.seq[r] = 0
    this.agentOfRow[r] = -1
    this.rowOf[agentNo & 0xffff] = -1
    this.free[this.freeN++] = r
  }

  /** RESET of a producer range [idBase, idBase + idCount) */
  clearRange(idBase: number, idCount: number): void {
    for (let r = 0; r < this.maxDrones; r++) {
      const a = this.agentOfRow[r]
      if (a >= idBase && a < idBase + idCount) this.clear(a)
    }
  }

  /** epoch change: everything */
  clearAll(): void {
    for (let r = 0; r < this.maxDrones; r++) if (this.agentOfRow[r] >= 0) this.clear(this.agentOfRow[r])
    this.blockStartS = Number.NaN
  }
}
