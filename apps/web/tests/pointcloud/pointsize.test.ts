// Point-size cap of non-leaf nodes (ADR-063; M05-FR-029, FR-030 revision, FX-WEB1): in dense (tau-limited) frames the
// non-leaf nodes clamp at min(rung.maxPx, max(4, ceil(2.5 sizeK tau))) so ancestors two or more levels above the frontier
// no longer cover the fine levels as maxPx discs ("bubbles"), while leaf nodes (no child in the data: where the data is
// exhausted near the camera) keep the rung's maxPx through the NodeTable leaf flag; Tier S rungs and `low` keep g02's 8.
import { describe, expect, it } from 'vitest'
import { NodeStore, type HierRecord } from '@/engine/pointcloud/core/NodeStore'
import { DrawTables } from '@/engine/pointcloud/gpu/DrawTable'
import { LADDER, PC, tauCapPx } from '@/engine/pointcloud/params'

describe('tauCapPx (ADR-063)', () => {
  it('per rung: g02 maxPx for tau >= 2 (Tier S rungs, low), tighter caps for medium, high and ultra', () => {
    const caps = LADDER.map((r) => tauCapPx(r))
    expect(caps).toEqual([8, 8, 8, 8, 6, 5, 4])
    for (let k = 0; k < LADDER.length; k++) expect(caps[k]).toBeLessThanOrEqual(LADDER[k].maxPx) // never above g02 §7.2
  })
  it('never below the Lite size of the frontier and its parent (< 2 sizeK tau)', () => {
    for (const r of LADDER) expect(tauCapPx(r)).toBeGreaterThanOrEqual(Math.min(r.maxPx, 2 * PC.sizeK * r.tau))
  })
})

/** root with 8 children; child 0 has 8 leaf grandchildren, children 1..7 are leaves */
function tree(): NodeStore {
  const recs: HierRecord[] = [{ type: 0, childMask: 255, numPoints: 100, byteOffset: 0, byteSize: 1200, parent: -1, child: 0, level: 0, name: 'r' }]
  for (let c = 0; c < 8; c++) recs.push({ type: 0, childMask: c === 0 ? 255 : 0, numPoints: 100, byteOffset: 0, byteSize: 1200, parent: 0, child: c, level: 1, name: `r${c}` })
  for (let c = 0; c < 8; c++) recs.push({ type: 0, childMask: 0, numPoints: 100, byteOffset: 0, byteSize: 1200, parent: 1, child: c, level: 2, name: `r0${c}` })
  return new NodeStore([{ cubeMin: [-50, -50, 0], cubeSize: 100, spacing: 100 / 64, records: recs, ext: null, octreeUrl: '' }])
}

describe('NodeTable leaf flag (ADR-063)', () => {
  it('texel 1 is (spacing_L, level, leaf, 0): leaf = 1 exactly for the nodes without a child in the data', () => {
    const t = tree()
    const tables = new DrawTables(t.N, 4096)
    tables.setNodes(t)
    for (let i = 0; i < t.N; i++) {
      let hasChild = false
      for (let c = 0; c < 8; c++) if (t.children[8 * i + c] >= 0) hasChild = true
      const o = 8 * i + 4
      expect(tables.nodeData[o]).toBeCloseTo(t.spacing[i], 5)
      expect(tables.nodeData[o + 1]).toBe(t.level[i])
      expect(tables.nodeData[o + 2], `node ${t.names[i]}`).toBe(hasChild ? 0 : 1)
      expect(tables.nodeData[o + 3]).toBe(0)
    }
    expect(t.N).toBe(17)
    // the root and r0 have children; the 7 level-1 leaves and the 8 level-2 leaves do not
    expect(Array.from({ length: t.N }, (_, i) => tables.nodeData[8 * i + 6]).reduce((a, b) => a + b, 0)).toBe(15)
    tables.dispose()
  })
})
