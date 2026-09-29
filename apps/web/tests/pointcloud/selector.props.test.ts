// M05-AC-001 (forest part), M05-AC-002: selector properties on the generated World Packages with their flight60 paths:
// drawn <= B on every frame of the six cities (suzhou at B = 10k, where the six L0 roots alone hold 20,327 points),
// achievedScreenError stays finite for the forest (roots keyed by their real error), the focus weights change the
// download order only (FR-014), and the first-screen depth cap bounds the first-frame target set (FR-004).
import { existsSync, readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { newScratch, newSelection, selectVisible, type SelectOptions } from '@/engine/pointcloud/core/Selector'
import { lodCameraLookAt, newLodCamera } from '@/engine/pointcloud/core/frustum'
import { collectCandidates, newCandidates, reorderWindow } from '@/engine/pointcloud/core/DownloadQueue'
import { LADDER, PC } from '@/engine/pointcloud/params'
import { haveWorlds, storeFromWorld } from './helpers'

const FL = new URL('../../public/bench/flight60/', import.meta.url)
const have = haveWorlds && existsSync(new URL('suzhou.bin', FL))
const opts = (B: number, tau: number): SelectOptions => ({ tau, B, headroom: PC.headroom, maxNodes: PC.maxNodes, maxSkips: PC.maxSkips, minPrefix: PC.minPrefix,
  hysteresis: PC.hysteresis, depthCap: 255, tauMinFrac: PC.tauMinFrac })

function flight(city: string): Float32Array {
  const b = readFileSync(new URL(`${city}.bin`, FL))
  return new Float32Array(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength))
}

describe.skipIf(!have)('selector properties on the World Packages', { timeout: 120_000 }, () => {
  it.each([['suzhou', 10_000], ['suzhou', 25_000], ['shenzhen', 10_000], ['newyork', 25_000], ['shanghai', 40_000], ['sanfrancisco', 25_000], ['chicago', 25_000]])(
    '%s B=%i: drawn <= B on every frame, finite achievedScreenError', (city, B) => {
      const { store: t } = storeFromWorld(city as string)
      const fl = flight(city as string)
      const S = newScratch(t.N)
      const sel = newSelection(PC.maxNodes)
      const cam = newLodCamera()
      let over = 0
      let nonFinite = 0
      for (let k = 0; k <= 3600; k += 2) {
        const o = 6 * k
        lodCameraLookAt([fl[o], fl[o + 1], fl[o + 2]], [fl[o + 3], fl[o + 4], fl[o + 5]], 60, 640, 360, 1, 20000, 360, cam)
        selectVisible(t, cam, opts(B as number, LADDER[0].tau), S, sel)
        for (let j = 0; j < sel.n; j++) t.drawnFrame[sel.idx[j]] = sel.frame
        if (sel.points > (B as number)) over++
        if (!Number.isFinite(sel.achieved)) nonFinite++
      }
      expect(over).toBe(0)
      expect(nonFinite).toBe(0)
    })

  it('the first admitted node is exempt only up to B: a forest at B = 10k draws a prefix, never more than B', () => {
    const { store: t } = storeFromWorld('suzhou')
    const cam = newLodCamera()
    lodCameraLookAt([0, -1500, 900], [0, 0, 0], 60, 640, 360, 1, 20000, 360, cam)
    const sel = selectVisible(t, cam, opts(10_000, 4), newScratch(t.N), newSelection(PC.maxNodes))
    expect(sel.points).toBeLessThanOrEqual(10_000)
    expect(sel.points).toBeGreaterThan(3_000)
    expect([0, 2]).toContain(sel.limitedBy) // the other roots are rejected by the budget or the bonus band
  })

  it('focus weights reorder the download window but never the selection (FR-014)', () => {
    const { store: t } = storeFromWorld('shenzhen')
    const cam = newLodCamera()
    lodCameraLookAt([-400, -600, 300], [0, 0, 0], 60, 640, 360, 1, 20000, 360, cam)
    const a = selectVisible(t, cam, opts(40_000, 4), newScratch(t.N), newSelection(PC.maxNodes))
    const b = selectVisible(t, cam, opts(40_000, 4), newScratch(t.N), newSelection(PC.maxNodes))
    expect(Array.from(b.idx.subarray(0, b.n))).toEqual(Array.from(a.idx.subarray(0, a.n)))
    const req = new Int32Array(t.N).fill(-1)
    const c1 = collectCandidates(a, t, 0, req, newCandidates(t.N))
    const plain = Array.from(c1.node.subarray(0, c1.n))
    const c2 = collectCandidates(a, t, 0, req, newCandidates(t.N))
    reorderWindow(c2, 8, t, cam, { mode: 'follow', p: Float64Array.from([300, 300, 0]) })
    const focused = Array.from(c2.node.subarray(0, c2.n))
    expect(focused.slice(8)).toEqual(plain.slice(8))
    expect([...focused.slice(0, 8)].sort((x, y) => x - y)).toEqual([...plain.slice(0, 8)].sort((x, y) => x - y))
    expect(focused.slice(0, 8)).not.toEqual(plain.slice(0, 8))
  })

  it('the first-screen depth cap bounds the first-frame target set (FR-004)', () => {
    const { store: t } = storeFromWorld('shenzhen')
    const cam = newLodCamera()
    lodCameraLookAt([-831.6, -1199.4, 899.6], [0, 0, 0], 50, 640, 360, 1, 20000, 360, cam)
    const s = selectVisible(t, cam, { ...opts(25_000, 4), depthCap: 1 }, newScratch(t.N), newSelection(PC.maxNodes))
    for (let k = 0; k < s.n; k++) expect(t.level[s.idx[k]]).toBeLessThanOrEqual(1)
    expect(s.points).toBeLessThanOrEqual(25_000)
    expect(s.points).toBeGreaterThan(15_000)
  })
})
