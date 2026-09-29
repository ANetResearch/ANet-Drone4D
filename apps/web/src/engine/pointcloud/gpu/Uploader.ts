// Uploader (M05 §6.6.3, M05-FR-023, FR-026; ADR-010, ADR-012). Owner: M05.
// Per frame, CPU-cached nodes of the current selection are copied into the PointPool in pop order (the selection is
// already ordered "selected first, most recently seen first, pop order"), up to the frame quota (Tier S 20k points,
// Tier B/A 8 MiB; the whole first-frame target set before the boot mask is revealed). The first node of a frame is never
// held back by the quota (a node bigger than the quota would starve); a later node that does not fit stops the frame.
// When the page allocator fails, nodes are evicted at once in eviction-policy order (the 1.5 B_ref band ignored) until
// the allocation succeeds; with no candidate left the frame stops uploading, poolStalls += 1, and the bytes stay cached.
import { NS, type NodeStore } from '../core/NodeStore'
import type { Selection } from '../core/Selector'
import type { PageAllocator } from './PageAllocator'

export interface UploadSink {
  /** copy n packed points to texel address base */
  copy(base: number, packed: Uint32Array, n: number): void
  /** evict for an allocation of n points; returns the number of nodes evicted (0 = no candidate left) */
  evictFor(n: number, nowMs: number): number
  packed(i: number): Uint32Array | null
  touched(i: number): void
}

export interface UploadResult {
  /** points uploaded this frame */
  used: number
  /** cached selected points still waiting */
  pending: number
  nodes: number
  /** the first node alone exceeded the quota */
  firstOverQuota: boolean
  stalled: boolean
}
export const newUploadResult = (): UploadResult => ({ used: 0, pending: 0, nodes: 0, firstOverQuota: false, stalled: false })

export function drainUploads(sel: Selection, t: NodeStore, quotaPts: number, nowMs: number, alloc: PageAllocator, sink: UploadSink, res: UploadResult): UploadResult {
  let used = 0
  let pending = 0
  let nodes = 0
  let stop = false
  let stalled = false
  let firstOver = false
  for (let k = 0; k < sel.n; k++) {
    const i = sel.idx[k]
    const n = t.numPoints[i]
    if (n === 0 || t.poolBase[i] >= 0 || t.cacheSlot[i] < 0) continue
    if (stop || (used > 0 && used + n > quotaPts)) {
      stop = true
      pending += n
      continue
    }
    const packed = sink.packed(i)
    if (!packed) continue
    let base = alloc.alloc(n)
    while (base < 0) {
      if (sink.evictFor(n, nowMs) === 0) break
      base = alloc.alloc(n)
    }
    if (base < 0) {
      stalled = true
      stop = true
      pending += n
      continue
    }
    sink.copy(base, packed, n)
    t.poolBase[i] = base
    t.state[i] = NS.RESIDENT
    t.residentSince[i] = nowMs
    t.fadeStart[i] = nowMs
    sink.touched(i)
    if (used === 0 && n > quotaPts) firstOver = true
    used += n
    nodes++
  }
  res.used = used
  res.pending = pending
  res.nodes = nodes
  res.firstOverQuota = firstOver
  res.stalled = stalled
  return res
}
