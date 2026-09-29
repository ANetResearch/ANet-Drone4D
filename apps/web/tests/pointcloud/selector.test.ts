// APH selector and PageAllocator (M05 §6.4, §6.6.2; ADR-009, ADR-010): budget respected, parents before children,
// frustum culling in the layer frame, deterministic pop order; first-fit pages with merge on free.
import { describe, expect, it } from 'vitest'
import { Matrix4, PerspectiveCamera } from 'three'
import { NodeStore, type HierRecord } from '@/engine/pointcloud/core/NodeStore'
import { newScratch, newSelection, selectVisible } from '@/engine/pointcloud/core/Selector'
import { makeLodCamera, newLodCamera } from '@/engine/pointcloud/core/frustum'
import { PageAllocator } from '@/engine/pointcloud/gpu/PageAllocator'

/** full octree of the given depth, n points per node */
function tree(depth: number, n: number): NodeStore {
  const recs: HierRecord[] = [{ type: 0, childMask: 255, numPoints: n, byteOffset: 0, byteSize: 12 * n, parent: -1, child: 0, level: 0, name: 'r' }]
  for (let i = 0; i < recs.length; i++) {
    const r = recs[i]
    if (r.level >= depth) {
      r.childMask = 0
      continue
    }
    for (let c = 0; c < 8; c++) recs.push({ type: 0, childMask: 255, numPoints: n, byteOffset: 0, byteSize: 12 * n, parent: i, child: c, level: r.level + 1, name: r.name + c })
  }
  return new NodeStore([{ cubeMin: [-500, -500, 0], cubeSize: 1000, spacing: 1000 / 64, records: recs, ext: null, octreeUrl: '' }])
}

/** camera in the three frame looking at an ENU point; the layer matrix is WorldRoot's rotation.x = -pi/2 */
function cam(eyeEnu: [number, number, number], targetEnu: [number, number, number]) {
  const c = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
  c.position.set(eyeEnu[0], eyeEnu[2], -eyeEnu[1])
  c.lookAt(targetEnu[0], targetEnu[2], -targetEnu[1])
  c.updateMatrixWorld()
  c.updateProjectionMatrix()
  const layer = new Matrix4().makeRotationX(-Math.PI / 2)
  return makeLodCamera(c, layer.elements, layer.clone().invert().elements, 720, newLodCamera())
}

const opts = (B: number) => ({ tau: 4, B, headroom: 0.15, maxNodes: 4096, maxSkips: 32, minPrefix: 512, hysteresis: 0.1, depthCap: 255, tauMinFrac: 0.25 })

describe('selectVisible (APH)', () => {
  const t = tree(3, 1000) // 585 nodes
  it('eye in the layer frame is the ENU position', () => {
    const lc = cam([100, -300, 250], [0, 0, 0])
    expect(Array.from(lc.eye).map((x: number) => Math.round(x * 1e6) / 1e6)).toEqual([100, -300, 250])
  })
  it('respects the budget, keeps parents before children and is deterministic', () => {
    const lc = cam([0, -900, 600], [0, 0, 0])
    const S = newScratch(t.N)
    const a = selectVisible(t, lc, opts(25_000), S, newSelection(4096))
    expect(a.points).toBeLessThanOrEqual(25_000)
    expect(a.n).toBeGreaterThan(8)
    expect([0, 2]).toContain(a.limitedBy) // budget or headroom (bonus band)
    const seen = new Set<number>()
    for (let k = 0; k < a.n; k++) {
      const i = a.idx[k]
      if (t.parent[i] >= 0) expect(seen.has(t.parent[i])).toBe(true)
      seen.add(i)
    }
    const b = selectVisible(t, lc, opts(25_000), newScratch(t.N), newSelection(4096))
    expect(Array.from(b.idx.subarray(0, b.n))).toEqual(Array.from(a.idx.subarray(0, a.n)))
  })
  it('culls nodes outside the frustum', () => {
    // looking east from far west at low altitude: nodes behind the camera (x > eye) never appear
    const lc = cam([-2000, 0, 50], [-3000, 0, 50])
    const s = selectVisible(t, lc, opts(1e6), newScratch(t.N), newSelection(4096))
    expect(s.n).toBe(0) // the root itself is culled (M05-FR-012)
  })
  it('depth cap limits the first-frame target set to the first-screen levels', () => {
    const lc = cam([0, -900, 600], [0, 0, 0])
    const s = selectVisible(t, lc, { ...opts(1e9), depthCap: 1 }, newScratch(t.N), newSelection(4096))
    for (let k = 0; k < s.n; k++) expect(t.level[s.idx[k]]).toBeLessThanOrEqual(1)
  })
})

describe('PageAllocator (256-texel pages, first fit, merge)', () => {
  it('allocates, frees, merges and tracks the high water mark', () => {
    const a = new PageAllocator(256 * 10)
    const x = a.alloc(300) // 2 pages
    const y = a.alloc(256) // 1 page
    const z = a.alloc(1000) // 4 pages
    expect([x, y, z]).toEqual([0, 512, 768])
    expect(a.usedPages).toBe(7)
    expect(a.alloc(1000)).toBe(-1) // 3 pages left
    a.free(y, 256)
    a.free(x, 300)
    expect(a.largestFree).toBe(3 * 256)
    expect(a.alloc(700)).toBe(0) // merged block of 3 pages at 0
    a.free(0, 700)
    a.free(z, 1000)
    expect(a.usedPages).toBe(0)
    expect(a.largestFree).toBe(10 * 256)
    expect(a.highWater).toBe(0)
  })
})
