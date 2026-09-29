// M05-AC-028 (Node part): hot-path functions do not allocate. selectVisible, buildDrawTable, cas.sample, planEviction and
// PageAllocator alloc/free each run 10^4 times after a warm-up; the V8 heap may not grow by more than 64 KB (gc forced
// before and after through --expose-gc set at run time).
import v8 from 'node:v8'
import vm from 'node:vm'
import { describe, expect, it } from 'vitest'
import { newScratch, newSelection, selectVisible } from '@/engine/pointcloud/core/Selector'
import { lodCameraLookAt, newLodCamera } from '@/engine/pointcloud/core/frustum'
import { buildDrawTable, newDrawBuild } from '@/engine/pointcloud/gpu/DrawTable'
import { CascadeController } from '@/engine/pointcloud/core/CascadeController'
import { newEvictionCandidates, planEviction } from '@/engine/pointcloud/core/EvictionPolicy'
import { PageAllocator } from '@/engine/pointcloud/gpu/PageAllocator'
import { PC } from '@/engine/pointcloud/params'
import { fixture } from './helpers'

v8.setFlagsFromString('--expose_gc')
const gc = vm.runInNewContext('gc') as () => void

function growth(fn: (k: number) => void, n = 10_000): number {
  for (let k = 0; k < 500; k++) fn(k) // warm-up (JIT, lazily created scratch)
  gc()
  gc()
  const before = process.memoryUsage().heapUsed
  for (let k = 0; k < n; k++) fn(k)
  gc()
  gc()
  return process.memoryUsage().heapUsed - before
}

describe('zero allocation on the hot path (M05-AC-028)', { timeout: 120_000 }, () => {
  const f = fixture('shenzhen')
  const t = f.store
  const cam = newLodCamera()
  lodCameraLookAt([-400, -600, 300], [0, 0, 0], 60, 1280, 720, 1, 20000, 720, cam)
  const S = newScratch(t.N)
  const sel = newSelection(PC.maxNodes)
  const o = { tau: 4, B: 40_000, headroom: PC.headroom, maxNodes: PC.maxNodes, maxSkips: PC.maxSkips, minPrefix: PC.minPrefix, hysteresis: PC.hysteresis, depthCap: 255,
    tauMinFrac: PC.tauMinFrac }
  it('selectVisible', () => {
    expect(growth(() => selectVisible(t, cam, o, S, sel))).toBeLessThanOrEqual(64 * 1024)
  })
  it('buildDrawTable', () => {
    for (let i = 0; i < t.N; i++) t.poolBase[i] = i * 256
    selectVisible(t, cam, o, S, sel)
    const out = new Uint32Array(PC.drawTableWidth * PC.drawTableRows * 4)
    const db = newDrawBuild()
    const ease = (x: number) => x
    expect(growth((k) => buildDrawTable(sel, t, k, 250, ease, false, out, db))).toBeLessThanOrEqual(64 * 1024)
  })
  it('cas.sample', () => {
    const c = new CascadeController({ startIndex: 0, floorIndex: 0, ceilIndex: 1, targetMs: 33.3, tailK: 2, initialB: 25_000, Bfloor: 20_000, poolCapacityPts: 253_952 })
    let now = 0
    expect(growth((k) => c.sample(k % 7 === 0 ? 50 : 33.3, (now += 33.3), 0, false))).toBeLessThanOrEqual(64 * 1024)
  })
  it('planEviction', () => {
    const c = newEvictionCandidates(600)
    for (let k = 0; k < 600; k++) {
      c.node[k] = k
      c.pts[k] = 1000 + (k % 17)
      c.level[k] = k % 5
      c.dist[k] = (k * 13) % 97
      c.outside[k] = k % 3 === 0 ? 1 : 0
    }
    c.n = 600
    const outIdx = new Int32Array(600)
    expect(growth((k) => planEviction(c, 700_000, 100_000, 5000 + k, outIdx), 2000)).toBeLessThanOrEqual(64 * 1024)
  })
  it('PageAllocator alloc and free', () => {
    const a = new PageAllocator(PC.poolWidth * PC.poolRowsS)
    const bases = new Int32Array(64)
    expect(growth((k) => {
      const j = k & 63
      if (bases[j] > 0) a.free(bases[j] - 1, 1000 + j * 10)
      bases[j] = a.alloc(1000 + j * 10) + 1
    })).toBeLessThanOrEqual(64 * 1024)
  })
})

describe('engine world and governor phases at steady state (M05 §9.5 item 5)', { timeout: 120_000 }, () => {
  it('update + sampleFrame allocate (almost) nothing once streaming settled', async () => {
    const { haveWorlds } = await import('./helpers')
    if (!haveWorlds) return
    const { makeRig, worldBase } = await import('./engineHarness')
    const r = makeRig()
    r.look([-400, -600, 300], [0, 0, 0])
    await r.engine.open(worldBase('shenzhen'))
    r.engine.markRevealed()
    await r.frames(240)
    const step = (): void => {
      r.ctx.frameNo++
      r.ctx.nowMs += 33.3
      r.engine.update(r.ctx)
      r.engine.sampleFrame(r.ctx)
    }
    // three's DataTexture.addUpdateRange pushes one small object per DrawTable row and frame (the only allocation left)
    expect(growth(step, 2000)).toBeLessThanOrEqual(256 * 1024)
    r.dispose()
  })
})
