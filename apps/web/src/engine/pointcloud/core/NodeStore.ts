// NodeStore: SoA node table built once per world, all roots in one global index space (M05 §6.2.1; AWR-16 §4.2, §4.8,
// §4.10). Owner: M05. Cubes are computed on the CPU in float64 from the Potree child order i = (x<<2)|(y<<1)|z (never
// accumulated on the GPU, AWR-03 §5.1 item 4). Tight boxes are subtree AABBs from hierarchy_ext.bin (cube when absent).

/** node states (M05 §6.11.2) */
export const NS = { UNLOADED: 0, FETCHING: 1, CACHED: 2, RESIDENT: 3, RETRY_WAIT: 4, FAILED: 5 } as const
export type NodeState = (typeof NS)[keyof typeof NS]

export interface HierRecord { type: number; childMask: number; numPoints: number; byteOffset: number; byteSize: number; parent: number; child: number; level: number; name: string }

export interface RootInput {
  cubeMin: readonly [number, number, number] | readonly number[]
  cubeSize: number
  spacing: number
  /** parsed hierarchy records in Potree BFS order */
  records: HierRecord[]
  /** hierarchy_ext.bin records (6 x u16 per node, normalised to the node cube), or null */
  ext: Uint16Array | null
  /** absolute tight boxes (6 float64 per node: min xyz, max xyz); tests and fixtures only, overrides ext */
  tight?: Float64Array | null
  /** URL of octree.bin for this root (with ?v=) */
  octreeUrl: string
}

export class NodeStore {
  readonly N: number
  readonly roots: Int32Array
  readonly rootOf: Uint8Array
  readonly level: Uint8Array
  readonly numPoints: Int32Array
  readonly byteOffset: Float64Array
  readonly byteSize: Float64Array
  readonly children: Int32Array
  readonly parent: Int32Array
  readonly cubeMin: Float64Array
  readonly cubeSize: Float64Array
  readonly tightMin: Float64Array
  readonly tightMax: Float64Array
  readonly spacing: Float64Array
  readonly state: Uint8Array
  readonly attempts: Uint8Array
  readonly retryAt: Float64Array
  /** frame (selection counter) in which the node was last selected; -10 initially */
  readonly lastSeen: Int32Array
  /** selection frame in which the node was last drawn (selected and resident); hysteresis and childDrawnMask */
  readonly drawnFrame: Int32Array
  readonly poolBase: Int32Array
  readonly residentSince: Float64Array
  readonly fadeStart: Float64Array
  readonly fade: Float32Array
  readonly cacheSlot: Int32Array
  readonly pinned: Uint8Array
  /** 1 once the node's bytes arrived at least once (uniqueBytes accounting) */
  readonly fetchedOnce: Uint8Array
  readonly names: string[]
  readonly rootUrl: string[]
  maxNodePoints = 0
  maxLevel = 0
  totalPoints = 0

  constructor(inputs: readonly RootInput[]) {
    let N = 0
    for (const r of inputs) N += r.records.length
    this.N = N
    this.roots = new Int32Array(inputs.length)
    this.rootOf = new Uint8Array(N)
    this.level = new Uint8Array(N)
    this.numPoints = new Int32Array(N)
    this.byteOffset = new Float64Array(N)
    this.byteSize = new Float64Array(N)
    this.children = new Int32Array(8 * N).fill(-1)
    this.parent = new Int32Array(N).fill(-1)
    this.cubeMin = new Float64Array(3 * N)
    this.cubeSize = new Float64Array(N)
    this.tightMin = new Float64Array(3 * N)
    this.tightMax = new Float64Array(3 * N)
    this.spacing = new Float64Array(N)
    this.state = new Uint8Array(N)
    this.attempts = new Uint8Array(N)
    this.retryAt = new Float64Array(N)
    this.lastSeen = new Int32Array(N).fill(-10)
    this.drawnFrame = new Int32Array(N).fill(-10)
    this.poolBase = new Int32Array(N).fill(-1)
    this.residentSince = new Float64Array(N)
    this.fadeStart = new Float64Array(N)
    this.fade = new Float32Array(N)
    this.cacheSlot = new Int32Array(N).fill(-1)
    this.pinned = new Uint8Array(N)
    this.fetchedOnce = new Uint8Array(N)
    this.names = new Array<string>(N)
    this.rootUrl = inputs.map((r) => r.octreeUrl)
    let g = 0
    inputs.forEach((r, ri) => {
      const base = g
      this.roots[ri] = base
      for (let k = 0; k < r.records.length; k++) {
        const rec = r.records[k]
        const i = base + k
        this.rootOf[i] = ri
        this.level[i] = rec.level
        this.numPoints[i] = rec.numPoints
        this.byteOffset[i] = rec.byteOffset
        this.byteSize[i] = rec.byteSize
        this.names[i] = rec.name
        this.totalPoints += rec.numPoints
        if (rec.numPoints > this.maxNodePoints) this.maxNodePoints = rec.numPoints
        if (rec.level > this.maxLevel) this.maxLevel = rec.level
        if (rec.parent >= 0) {
          const p = base + rec.parent
          this.parent[i] = p
          this.children[8 * p + rec.child] = i
          const size = this.cubeSize[p] / 2
          this.cubeSize[i] = size
          this.cubeMin[3 * i] = this.cubeMin[3 * p] + size * ((rec.child >> 2) & 1)
          this.cubeMin[3 * i + 1] = this.cubeMin[3 * p + 1] + size * ((rec.child >> 1) & 1)
          this.cubeMin[3 * i + 2] = this.cubeMin[3 * p + 2] + size * (rec.child & 1)
        } else {
          this.cubeSize[i] = r.cubeSize
          this.cubeMin[3 * i] = r.cubeMin[0]
          this.cubeMin[3 * i + 1] = r.cubeMin[1]
          this.cubeMin[3 * i + 2] = r.cubeMin[2]
        }
        this.spacing[i] = r.spacing / 2 ** rec.level
        // nodes without own points are never fetched nor uploaded: they count as resident and faded in (M05 §6.5.2)
        if (rec.numPoints === 0) this.fade[i] = 1
        const s = this.cubeSize[i]
        for (let a = 0; a < 3; a++) {
          const m = this.cubeMin[3 * i + a]
          if (r.tight) {
            this.tightMin[3 * i + a] = r.tight[6 * k + a]
            this.tightMax[3 * i + a] = r.tight[6 * k + 3 + a]
          } else if (r.ext) {
            this.tightMin[3 * i + a] = m + (r.ext[6 * k + a] / 65535) * s
            this.tightMax[3 * i + a] = m + (r.ext[6 * k + 3 + a] / 65535) * s
          } else {
            this.tightMin[3 * i + a] = m
            this.tightMax[3 * i + a] = m + s
          }
        }
      }
      g += r.records.length
    })
  }

  /** root index range [start, end) of root r */
  rootRange(r: number): [number, number] {
    return [this.roots[r], r + 1 < this.roots.length ? this.roots[r + 1] : this.N]
  }

  /** node index by Potree name within a root (tests and debugging) */
  indexOf(name: string, root = 0): number {
    const [a, b] = this.rootRange(root)
    for (let i = a; i < b; i++) if (this.names[i] === name) return i
    return -1
  }

  /** reset the per-session streaming state (world stays, GPU and CPU residency cleared) */
  resetStreaming(): void {
    this.state.fill(NS.UNLOADED)
    this.attempts.fill(0)
    this.retryAt.fill(0)
    this.lastSeen.fill(-10)
    this.drawnFrame.fill(-10)
    this.poolBase.fill(-1)
    this.cacheSlot.fill(-1)
    this.pinned.fill(0)
    for (let i = 0; i < this.N; i++) this.fade[i] = this.numPoints[i] === 0 ? 1 : 0
  }
}
