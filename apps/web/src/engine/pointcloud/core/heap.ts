// Parallel typed-array max-heap (node, key, containment) for the APH selector (M05 §6.4; g02 lod.mjs hpush/hpop, same
// sift order so pop sequences match the prototype bit for bit). Owner: M05. Zero allocation after construction.
export class NodeHeap {
  readonly hn: Int32Array
  readonly hk: Float64Array
  readonly hc: Uint8Array
  size = 0
  /** last popped node, key and containment flag */
  pn = 0
  pk = 0
  pc = 0

  constructor(capacity: number) {
    this.hn = new Int32Array(capacity)
    this.hk = new Float64Array(capacity)
    this.hc = new Uint8Array(capacity)
  }

  clear(): void {
    this.size = 0
  }

  get topKey(): number {
    return this.hk[0]
  }

  push(i: number, k: number, c: number): void {
    const hn = this.hn
    const hk = this.hk
    const hc = this.hc
    let j = this.size++
    while (j > 0) {
      const p = (j - 1) >> 1
      if (hk[p] >= k) break
      hn[j] = hn[p]
      hk[j] = hk[p]
      hc[j] = hc[p]
      j = p
    }
    hn[j] = i
    hk[j] = k
    hc[j] = c
  }

  /** pops the maximum into pn, pk, pc */
  pop(): void {
    const hn = this.hn
    const hk = this.hk
    const hc = this.hc
    this.pn = hn[0]
    this.pk = hk[0]
    this.pc = hc[0]
    const n = --this.size
    if (n === 0) return
    const li = hn[n]
    const lk = hk[n]
    const lc = hc[n]
    let j = 0
    for (;;) {
      let c = 2 * j + 1
      if (c >= n) break
      if (c + 1 < n && hk[c + 1] > hk[c]) c++
      if (hk[c] <= lk) break
      hn[j] = hn[c]
      hk[j] = hk[c]
      hc[j] = hc[c]
      j = c
    }
    hn[j] = li
    hk[j] = lk
    hc[j] = lc
  }
}

type Cmp = (a: number, b: number) => number

function siftDown(idx: Int32Array, start: number, end: number, cmp: Cmp): void {
  let root = start
  for (;;) {
    let child = 2 * root + 1
    if (child > end) return
    if (child + 1 <= end && cmp(idx[child], idx[child + 1]) < 0) child++
    if (cmp(idx[root], idx[child]) >= 0) return
    const t = idx[root]
    idx[root] = idx[child]
    idx[child] = t
    root = child
  }
}

/**
 * In-place heap sort of idx[0..n) by a comparator over node indices (negative: a before b). Used by the eviction and
 * CPU cache planners; no allocation (Array.prototype.sort on typed arrays may copy). Pass a module-level comparator.
 */
export function sortIdx(idx: Int32Array, n: number, cmp: Cmp): void {
  for (let s = (n >> 1) - 1; s >= 0; s--) siftDown(idx, s, n - 1, cmp)
  for (let end = n - 1; end > 0; end--) {
    const t = idx[0]
    idx[0] = idx[end]
    idx[end] = t
    siftDown(idx, 0, end - 1, cmp)
  }
}
