// CPU cache of packed node texels, byte-bounded LRU (ADR-010; M05 §6.6.1, §6.2.5, M05-FR-021). Owner: M05.
// Slot = node index (the pool of slots has the node count as capacity, no object per entry). Over the limit it evicts
// down to 0.9 x limit. Never evicted: pinned first-screen nodes, GPU-resident nodes (residency invariant: poolBase >= 0
// implies cacheSlot >= 0, so a context loss re-uploads without re-downloading and picking decodes without requests),
// nodes of the protected (current) selection, which covers the nodes queued for upload. Order: lastUsed ascending
// (least recently selected or uploaded first), then deeper first, then the node index. Eviction only drops the
// reference. Pure bookkeeping: frame numbers are passed in.
import { NS, type NodeStore } from './NodeStore'
import { sortIdx } from './heap'
import { PC } from '../params'

let LU: Int32Array = new Int32Array(0)
let LV: Uint8Array = new Uint8Array(0)
function cmp(a: number, b: number): number {
  return LU[a] - LU[b] || LV[b] - LV[a] || a - b
}

export class CpuCache {
  readonly packed: (Uint32Array | null)[]
  readonly lastUsed: Int32Array
  bytes = 0
  peak = 0
  evictions = 0
  private readonly order: Int32Array

  constructor(readonly t: NodeStore, readonly limitBytes: number) {
    this.packed = new Array<Uint32Array | null>(t.N).fill(null)
    this.lastUsed = new Int32Array(t.N).fill(-1)
    this.order = new Int32Array(t.N)
  }

  has(i: number): boolean {
    return this.packed[i] !== null
  }
  get(i: number): Uint32Array | null {
    return this.packed[i]
  }

  /** store packed texels of node i (state CACHED unless already resident); pinned for first-screen nodes */
  put(i: number, buf: Uint32Array, frame: number, pinned = false): void {
    const t = this.t
    const prev = this.packed[i]
    if (prev) this.bytes -= prev.byteLength
    this.packed[i] = buf
    this.bytes += buf.byteLength
    if (this.bytes > this.peak) this.peak = this.bytes
    this.lastUsed[i] = frame
    t.cacheSlot[i] = i
    if (pinned) t.pinned[i] = 1
    if (t.poolBase[i] < 0) t.state[i] = NS.CACHED
  }

  touch(i: number, frame: number): void {
    if (this.packed[i]) this.lastUsed[i] = frame
  }

  /**
   * Evict down to 0.9 x limit when over the limit. protectFrame: nodes with t.lastSeen === protectFrame (the current
   * selection) are kept. Returns the number of evicted nodes.
   */
  trim(protectFrame: number): number {
    if (this.bytes <= this.limitBytes) return 0
    const t = this.t
    const target = PC.cpuCacheEvictTo * this.limitBytes
    let m = 0
    for (let i = 0; i < t.N; i++) {
      if (!this.packed[i] || t.pinned[i] || t.poolBase[i] >= 0 || t.lastSeen[i] === protectFrame) continue
      this.order[m++] = i
    }
    LU = this.lastUsed
    LV = t.level
    sortIdx(this.order, m, cmp)
    let k = 0
    for (let j = 0; j < m && this.bytes > target; j++) {
      this.drop(this.order[j])
      k++
    }
    this.evictions += k
    return k
  }

  /** drop node i from the cache (it goes back to UNLOADED) */
  drop(i: number): void {
    const t = this.t
    const b = this.packed[i]
    if (!b) return
    this.bytes -= b.byteLength
    this.packed[i] = null
    t.cacheSlot[i] = -1
    if (t.poolBase[i] < 0 && t.state[i] === NS.CACHED) t.state[i] = NS.UNLOADED
  }

  clear(): void {
    this.packed.fill(null)
    this.lastUsed.fill(-1)
    this.bytes = 0
  }
}
