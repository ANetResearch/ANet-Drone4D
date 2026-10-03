// First-screen rule R and the BFS prefix (ADR-013; AWR-16 §4.11 six-city table; M05-AC-004), hierarchy parsing of the
// generated worlds (AWR-16 §4.2, §4.8), and the openWorld request sequence with one Range per root (AWR-17 §5.5).
import { existsSync, readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { firstScreenBytes, firstScreenLevel } from '@/engine/pointcloud/io/firstScreen'
import { parseHierarchy, parseHierarchyExt } from '@/engine/pointcloud/io/hierarchy'
import { NodeStore } from '@/engine/pointcloud/core/NodeStore'
import { Fetcher } from '@/engine/pointcloud/io/fetcher'
import { openWorld } from '@/engine/pointcloud/io/openWorld'
import { LADDER, PC } from '@/engine/pointcloud/params'
import type { PotreeMeta, WorldJson } from '@/engine/pointcloud/types'

const WORLDS = new URL('../../../../worlds/', import.meta.url)
const have = existsSync(new URL('shenzhen/world.json', WORLDS))
const json = <T>(rel: string): T => JSON.parse(readFileSync(new URL(rel, WORLDS), 'utf8')) as T
function roots(city: string): { href: string; md: PotreeMeta }[] {
  const w = json<WorldJson>(`${city}/world.json`)
  const layer = w.layers.find((l) => l.type === 'pointcloud' && l.default)!
  return layer.roots!.map((r) => ({ href: r.href, md: json<PotreeMeta>(`${city}/${r.href}metadata.json`) }))
}
const levels = (md: PotreeMeta) => ({ levelsPoints: md.anet.levelsPoints, levelsByteEnd: md.anet.levelsByteEnd, depth: md.hierarchy.depth })

// AWR-16 §4.11 table: [Tier S level, points, bytes], [Tier B/A level, points, bytes]
const TABLE: Record<string, [[number, number, number], [number, number, number]]> = {
  shenzhen: [[1, 26_782, 321_384], [2, 124_673, 1_496_076]],
  shanghai: [[2, 75_154, 901_848], [3, 418_818, 5_025_816]],
  newyork: [[1, 35_351, 424_212], [2, 181_771, 2_181_252]],
  sanfrancisco: [[1, 25_559, 306_708], [2, 108_268, 1_299_216]],
  suzhou: [[0, 20_327, 243_924], [1, 108_263, 1_299_156]],
  chicago: [[2, 76_541, 918_492], [3, 352_915, 4_234_980]],
}

describe.skipIf(!have)('rule R on the six cities (AWR-16 §4.11)', () => {
  const poolS = PC.poolWidth * PC.poolRowsS
  const poolI = PC.poolWidth * PC.poolRowsIgpu
  it.each(Object.keys(TABLE))('%s', (city) => {
    const rs = roots(city)
    const lv = rs.map((r) => levels(r.md))
    const [s, b] = TABLE[city]
    const Ls = firstScreenLevel(lv, poolS, LADDER[0].hi)
    expect(Ls).toBe(s[0])
    expect(lv.reduce((a, r) => a + r.levelsPoints[Ls], 0)).toBe(s[1])
    expect(lv.reduce((a, r) => a + firstScreenBytes(r, Ls), 0)).toBe(s[2])
    const Lb = firstScreenLevel(lv, poolI, LADDER[3].hi) // iGPU start rung "low"
    expect(Lb).toBe(b[0])
    expect(lv.reduce((a, r) => a + r.levelsPoints[Lb], 0)).toBe(b[1])
    expect(lv.reduce((a, r) => a + firstScreenBytes(r, Lb), 0)).toBe(b[2])
  })
  it('falls back to level 0 when even L0 exceeds the cap', () => {
    expect(firstScreenLevel([{ levelsPoints: [5e6, 6e6], levelsByteEnd: [1, 2], depth: 1 }], 1000, 1000)).toBe(0)
  })
})

describe.skipIf(!have)('hierarchy.bin of shenzhen', () => {
  if (!have) return // describe.skipIf still runs this body while collecting; the reads below need the built world (SHOW-CI)
  const md = json<PotreeMeta>('shenzhen/visual/pointcloud/metadata.json')
  const hb = readFileSync(new URL('shenzhen/visual/pointcloud/hierarchy.bin', WORLDS))
  const xb = readFileSync(new URL('shenzhen/visual/pointcloud/hierarchy_ext.bin', WORLDS))
  const ab = (b: Buffer): ArrayBuffer => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer
  const recs = parseHierarchy(ab(hb), md.hierarchy.firstChunkSize)
  const store = new NodeStore([{ cubeMin: md.boundingBox.min, cubeSize: md.boundingBox.max[0] - md.boundingBox.min[0], spacing: md.spacing, records: recs,
    ext: parseHierarchyExt(ab(xb), recs.length), octreeUrl: 'x' }])

  it('node count, level histogram and level byte ends match metadata', () => {
    expect(recs.length).toBe(md.anet.nodeCount)
    const perLevel = new Array<number>(md.hierarchy.depth + 1).fill(0)
    const ptsLevel = new Array<number>(md.hierarchy.depth + 1).fill(0)
    let end = new Array<number>(md.hierarchy.depth + 1).fill(0)
    for (const r of recs) {
      perLevel[r.level]++
      ptsLevel[r.level] += r.numPoints
      end[r.level] = Math.max(end[r.level], r.byteOffset + r.byteSize)
    }
    expect(perLevel).toEqual(md.anet.levelsNodes)
    let acc = 0
    expect(ptsLevel.map((p) => (acc += p))).toEqual(md.anet.levelsPoints)
    end = end.map((e, i) => Math.max(e, i > 0 ? end[i - 1] : 0))
    expect(end).toEqual(md.anet.levelsByteEnd)
  })

  it('BFS contiguous payloads: offsets are level-ordered, 4-aligned and byteSize = 12 n', () => {
    let prev = 0
    for (const r of recs) {
      if (r.numPoints === 0) continue
      expect(r.byteOffset).toBeGreaterThanOrEqual(prev)
      expect(r.byteOffset % 4).toBe(0)
      expect(r.byteSize).toBe(12 * r.numPoints)
      prev = r.byteOffset + r.byteSize
    }
  })

  it('cubes halve per level and the subtree tight boxes stay inside the cube', () => {
    const i = store.indexOf('r04')
    expect(i).toBeGreaterThan(0)
    const p = store.parent[i]
    expect(store.cubeSize[i]).toBeCloseTo(store.cubeSize[p] / 2, 9)
    expect(store.cubeSize[i]).toBeCloseTo(store.cubeSize[0] / 4, 9)
    for (let k = 0; k < store.N; k++) {
      for (let a = 0; a < 3; a++) {
        expect(store.tightMin[3 * k + a]).toBeGreaterThanOrEqual(store.cubeMin[3 * k + a] - 1e-6)
        expect(store.tightMax[3 * k + a]).toBeLessThanOrEqual(store.cubeMin[3 * k + a] + store.cubeSize[k] + 1e-6)
      }
    }
    // root tight box within one quantisation step of anet.tightBounds (V-P-17)
    const q = store.cubeSize[0] / 65535
    for (let a = 0; a < 3; a++) {
      expect(Math.abs(store.tightMin[a] - md.anet.tightBounds.min[a])).toBeLessThanOrEqual(q + 1e-9)
      expect(Math.abs(store.tightMax[a] - md.anet.tightBounds.max[a])).toBeLessThanOrEqual(q + 1e-9)
    }
  })
})

describe.skipIf(!have)('openWorld request sequence (AWR-17 §5.5)', () => {
  it('world.json no-cache, ?v= on the rest, whole hierarchy, one Range for the first screen', async () => {
    const calls: { url: string; range: string | null; cache?: string }[] = []
    const f = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      const range = new Headers(init?.headers).get('Range')
      calls.push({ url, range, cache: init?.cache })
      const u = new URL(url)
      const rel = decodeURIComponent(u.pathname.replace(/^\/worlds\//, ''))
      const body = readFileSync(new URL(rel, WORLDS))
      if (range) {
        const m = /^bytes=(\d+)-(\d+)$/.exec(range)!
        const part = body.subarray(Number(m[1]), Number(m[2]) + 1)
        return new Response(part, { status: 206, headers: { 'Content-Range': `bytes ${m[1]}-${m[2]}/${body.byteLength}` } })
      }
      return new Response(body, { status: 200 })
    }) as typeof fetch
    const fetcher = new Fetcher({ workers: 1, limit: 4, useWorker: false, f })
    const w = await openWorld('http://x/worlds/shenzhen/', { fetcher, poolCapacityPts: PC.poolWidth * PC.poolRowsS, startHi: LADDER[0].hi, now: () => 0, f })
    expect(calls[0].url).toBe('http://x/worlds/shenzhen/world.json')
    expect(calls[0].cache).toBe('no-cache')
    const cv = w.world.contentVersion
    for (const c of calls.slice(1)) expect(c.url.endsWith(`?v=${cv}`)).toBe(true)
    const ranges = calls.filter((c) => c.range !== null)
    expect(ranges).toHaveLength(1)
    expect(ranges[0].range).toBe('bytes=0-321383')
    expect(calls.find((c) => c.url.includes('hierarchy.bin'))!.range).toBeNull()
    expect(w.firstScreenLevel).toBe(1)
    expect(w.firstScreenBytes).toBe(321_384)
    let pts = 0
    for (const p of w.firstScreen) pts += p.buf.length / 4
    expect(pts).toBe(26_782)
    fetcher.dispose()
  })
})
