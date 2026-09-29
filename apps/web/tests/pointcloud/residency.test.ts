// M05-AC-015 (unit part), FR-021, FR-023, FR-026, FR-031: page allocator (1e5 random operations, merging, high water),
// CPU cache LRU, uploader quota semantics and allocation failure handling, DrawTable fade and childDrawnMask.
import { describe, expect, it } from 'vitest'
import { PageAllocator } from '@/engine/pointcloud/gpu/PageAllocator'
import { CpuCache } from '@/engine/pointcloud/core/CpuCache'
import { NodeStore, NS, type HierRecord } from '@/engine/pointcloud/core/NodeStore'
import { newSelection, type Selection } from '@/engine/pointcloud/core/Selector'
import { drainUploads, newUploadResult, type UploadSink } from '@/engine/pointcloud/gpu/Uploader'
import { buildDrawTable, newDrawBuild } from '@/engine/pointcloud/gpu/DrawTable'
import { PC } from '@/engine/pointcloud/params'
import { rng } from './helpers'

describe('PageAllocator (M05-AC-015)', { timeout: 60_000 }, () => {
  it('1e5 random allocations and frees: no overlap, full merge, high water back to 0', () => {
    const pages = 2000
    const a = new PageAllocator(pages * 256)
    const r = rng(11)
    const live: [number, number][] = []
    const owner = new Int32Array(pages).fill(-1)
    let overlaps = 0
    for (let op = 0; op < 100_000; op++) {
      if (live.length === 0 || (r() < 0.55 && live.length < 400)) {
        const n = 1 + Math.floor(r() * 20_000)
        const base = a.alloc(n)
        if (base < 0) continue
        const p0 = base / 256
        const np = Math.ceil(n / 256)
        for (let p = p0; p < p0 + np; p++) {
          if (owner[p] !== -1) overlaps++
          owner[p] = op
        }
        live.push([base, n])
      } else {
        const k = Math.floor(r() * live.length)
        const [base, n] = live[k]
        live.splice(k, 1)
        a.free(base, n)
        for (let p = base / 256; p < base / 256 + Math.ceil(n / 256); p++) owner[p] = -1
      }
    }
    expect(overlaps).toBe(0)
    for (const [base, n] of live) a.free(base, n)
    expect(a.usedPages).toBe(0)
    expect(a.largestFree).toBe(pages * 256)
    expect(a.highWater).toBe(0)
  })
})

function flatTree(nodes: number[], childrenOf: number[][] = []): NodeStore {
  const recs: HierRecord[] = nodes.map((n, i) => ({ type: 0, childMask: 0, numPoints: n, byteOffset: 0, byteSize: 12 * n, parent: -1, child: 0, level: 0, name: `n${i}` }))
  childrenOf.forEach((ch, p) => ch.forEach((c, k) => {
    recs[c].parent = p
    recs[c].child = k
    recs[c].level = recs[p].level + 1
    recs[p].childMask |= 1 << k
  }))
  return new NodeStore([{ cubeMin: [0, 0, 0], cubeSize: 1, spacing: 1, records: recs, ext: null, octreeUrl: '' }])
}
function selOf(idx: number[], cnt: number[], frame = 1): Selection {
  const s = newSelection(64)
  idx.forEach((i, k) => {
    s.idx[k] = i
    s.cnt[k] = cnt[k]
  })
  s.n = idx.length
  s.points = cnt.reduce((a, b) => a + b, 0)
  s.frame = frame
  return s
}

describe('CpuCache (M05-FR-021)', () => {
  it('evicts to 0.9 x limit, oldest first, then deeper; never pinned, resident or selected nodes', () => {
    const t = flatTree(Array(10).fill(1000))
    const c = new CpuCache(t, 16 * 1000 * 5) // 5 nodes of 16 kB
    for (let i = 0; i < 7; i++) {
      t.lastSeen[i] = i
      c.put(i, new Uint32Array(4000), i, i === 0)
    }
    t.poolBase[1] = 0
    t.state[1] = NS.RESIDENT
    t.lastSeen[6] = 99
    const k = c.trim(99)
    expect(c.bytes).toBeLessThanOrEqual(0.9 * c.limitBytes)
    expect(k).toBe(3)
    expect([0, 1, 6].every((i) => c.has(i))).toBe(true) // pinned, resident, selected
    expect([2, 3, 4].every((i) => !c.has(i))).toBe(true) // oldest evicted
    expect(t.state[2]).toBe(NS.UNLOADED)
    expect(t.cacheSlot[2]).toBe(-1)
    expect(c.peak).toBe(7 * 16_000)
  })
})

function sinkFor(t: NodeStore, c: CpuCache, evict: (n: number) => number = () => 0): UploadSink & { copies: number[] } {
  const copies: number[] = []
  return { copies, copy: (_b, _p, n) => copies.push(n), evictFor: (n) => evict(n), packed: (i) => c.get(i), touched: () => {} }
}

describe('Uploader quota (M05-FR-023, FR-026)', () => {
  it('first node is never held back; later nodes that do not fit stop the frame', () => {
    const t = flatTree([30_000, 8000, 8000, 8000])
    const c = new CpuCache(t, 1e9)
    for (let i = 0; i < 4; i++) c.put(i, new Uint32Array(4 * t.numPoints[i]), 0)
    const a = new PageAllocator(PC.poolWidth * PC.poolRowsS)
    const sink = sinkFor(t, c)
    const res = drainUploads(selOf([0, 1, 2, 3], [30_000, 8000, 8000, 8000]), t, 20_000, 0, a, sink, newUploadResult())
    expect(res.used).toBe(30_000)
    expect(res.firstOverQuota).toBe(true)
    expect(res.pending).toBe(24_000)
    const res2 = drainUploads(selOf([0, 1, 2, 3], [30_000, 8000, 8000, 8000]), t, 20_000, 0, a, sink, newUploadResult())
    expect(res2.used).toBe(16_000)
    expect(res2.firstOverQuota).toBe(false)
    expect(t.state[1]).toBe(NS.RESIDENT)
    expect(t.fadeStart[1]).toBe(0)
  })
  it('allocation failure evicts until it fits; with no candidate left the frame stalls and the bytes stay cached', () => {
    const t = flatTree([600, 600, 600])
    const c = new CpuCache(t, 1e9)
    for (let i = 0; i < 3; i++) c.put(i, new Uint32Array(4 * 600), 0)
    const a = new PageAllocator(256 * 6) // room for two 3-page nodes
    let evictions = 0
    const sink = sinkFor(t, c, () => {
      if (t.poolBase[0] < 0) return 0
      a.free(t.poolBase[0], 600)
      t.poolBase[0] = -1
      t.state[0] = NS.CACHED
      evictions++
      return 1
    })
    drainUploads(selOf([0, 1], [600, 600]), t, 1e9, 0, a, sink, newUploadResult())
    const res = drainUploads(selOf([2], [600]), t, 1e9, 0, a, sink, newUploadResult())
    expect(evictions).toBe(1)
    expect(res.stalled).toBe(false)
    expect(t.poolBase[2]).toBeGreaterThanOrEqual(0)
    const t2 = flatTree([600, 600, 600])
    const c2 = new CpuCache(t2, 1e9)
    for (let i = 0; i < 3; i++) c2.put(i, new Uint32Array(4 * 600), 0)
    const a2 = new PageAllocator(256 * 3)
    const res2 = drainUploads(selOf([0, 1], [600, 600]), t2, 1e9, 0, a2, sinkFor(t2, c2), newUploadResult())
    expect(res2.stalled).toBe(true)
    expect(res2.pending).toBe(600)
    expect(c2.has(1)).toBe(true)
  })
})

describe('DrawTable, fade and childDrawnMask (M05-FR-028, FR-029, FR-031)', () => {
  const ease = (x: number) => x * x * (3 - 2 * x)
  it('entries of selected resident nodes in pop order; prefix counts; hysteresis marks', () => {
    const t = flatTree([100, 50, 30, 0], [[1, 2, 3]])
    t.poolBase[0] = 0
    t.poolBase[1] = 256
    const out = new Uint32Array(PC.drawTableWidth * 16)
    const b = buildDrawTable(selOf([0, 1, 2, 3], [100, 20, 30, 0], 5), t, 1000, 250, ease, true, out, newDrawBuild())
    expect(b.k).toBe(2)
    expect(b.drawn).toBe(120)
    expect([out[0], out[1], out[2] & 0xffffff, out[3] & 0xffffff]).toEqual([0, 0, 100, 0])
    expect([out[4], out[5], out[6] & 0xffffff, out[7] & 0xffffff]).toEqual([100, 256, 20, 1])
    expect([t.drawnFrame[0], t.drawnFrame[1], t.drawnFrame[2], t.drawnFrame[3]]).toEqual([5, 5, -10, 5]) // node 2 not resident; node 3 has no points
    // children 1 (resident, reduced motion: faded) and 3 (no points) are drawn: parent mask bits 0 and 2
    expect(out[2] >>> 24).toBe(0b101)
  })
  it('fade rises monotonically over --duration-lod-fade and the parent octant shrinks only once the child reached 1', () => {
    const t = flatTree([100, 50], [[1]])
    t.poolBase[0] = 0
    t.poolBase[1] = 256
    t.fadeStart[1] = 1000
    const out = new Uint32Array(PC.drawTableWidth * 16)
    let prev = -1
    for (let now = 1000, f = 1; now <= 1300; now += 25, f++) {
      buildDrawTable(selOf([0, 1], [100, 50], f), t, now, 250, ease, false, out, newDrawBuild())
      const fade = (out[7] >>> 24) / 255
      expect(fade).toBeGreaterThanOrEqual(prev)
      prev = fade
      expect(out[2] >>> 24).toBe(now - 1000 >= 250 ? 1 : 0)
    }
    expect(prev).toBe(1)
  })
})
