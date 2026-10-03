// M05-FR-002 (every cheap check rejects a tampered world with 401), FR-019 (a synthetic paged hierarchy expands to the
// single-chunk tree), FR-008 (409 during open -> 402), FR-003 forest first screen (suzhou: 6 Ranges, at most 4 at once).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { checkHierarchyLength, checkMetaCheap, checkOctreeTotal, checkWorldCheap, PC_FORMAT_UNSUPPORTED } from '@/engine/pointcloud/io/meta'
import { HIER_RECORD, parseChunk, parseHierarchy, parseHierarchyPaged } from '@/engine/pointcloud/io/hierarchy'
import { openWorld } from '@/engine/pointcloud/io/openWorld'
import { Fetcher } from '@/engine/pointcloud/io/fetcher'
import { LADDER, PC } from '@/engine/pointcloud/params'
import type { PotreeMeta, WorldJson } from '@/engine/pointcloud/types'
import { WORLDS, haveWorlds } from './helpers'
import { WorldServer } from './engineHarness'

const json = <T>(rel: string): T => JSON.parse(readFileSync(new URL(rel, WORLDS), 'utf8')) as T

describe.skipIf(!haveWorlds)('cheap checks (M05-FR-002)', () => {
  if (!haveWorlds) return // describe.skipIf still runs this body while collecting; the reads below need the built world (SHOW-CI)
  const md = json<PotreeMeta>('shenzhen/visual/pointcloud/metadata.json')
  const w = json<WorldJson>('shenzhen/world.json')
  const root = { points: md.points, depth: md.hierarchy.depth }
  const clone = (): PotreeMeta => JSON.parse(JSON.stringify(md)) as PotreeMeta
  const rejects = (f: () => void): void => {
    try {
      f()
    } catch (e) {
      expect((e as { code?: number }).code).toBe(PC_FORMAT_UNSUPPORTED)
      return
    }
    throw new Error('not rejected')
  }
  it('the generated world passes', () => {
    expect(() => checkWorldCheap(w)).not.toThrow()
    expect(() => checkMetaCheap(md, root)).not.toThrow()
  })
  const tampers: [string, (m: PotreeMeta) => void][] = [
    ['version', (m) => (m.version = '1.8')],
    ['encoding', (m) => (m.encoding = 'DEFAULT')],
    ['formatVersion', (m) => (m.anet.formatVersion = 2)],
    ['bytesPerPoint', (m) => (m.anet.bytesPerPoint = 20 as 12)],
    ['streams', (m) => (m.anet.streams = ['pos'])],
    ['compression', (m) => (m.anet.compression = 'zstd' as 'none')],
    ['attributes', (m) => (m.attributes[0].name = 'position')],
    ['cube', (m) => (m.boundingBox.max[2] += 5)],
    ['levelsByteEnd length', (m) => m.anet.levelsByteEnd.pop()],
    ['levelsByteEnd order', (m) => (m.anet.levelsByteEnd[1] = 0)],
    ['firstChunkSize', (m) => (m.hierarchy.firstChunkSize += 3)],
    ['roots[] points', (m) => (m.points += 1)],
  ]
  it.each(tampers)('%s', (_name, f) => {
    const m = clone()
    f(m)
    rejects(() => checkMetaCheap(m, root))
  })
  it('world.json major version, hierarchy length and octree total', () => {
    rejects(() => checkWorldCheap({ ...w, schemaVersion: '2.0.0' }))
    rejects(() => checkHierarchyLength(15_313))
    rejects(() => checkOctreeTotal(md, 1000))
    expect(() => checkOctreeTotal(md, md.anet.levelsByteEnd.at(-1)!)).not.toThrow()
  })
})

/** encode records as a Potree hierarchy chunk */
function encode(recs: { type: number; mask: number; n: number; off: number; size: number }[]): ArrayBuffer {
  const b = new ArrayBuffer(recs.length * HIER_RECORD)
  const dv = new DataView(b)
  recs.forEach((r, i) => {
    const o = i * HIER_RECORD
    dv.setUint8(o, r.type)
    dv.setUint8(o + 1, r.mask)
    dv.setUint32(o + 2, r.n, true)
    dv.setBigInt64(o + 6, BigInt(r.off), true)
    dv.setBigInt64(o + 14, BigInt(r.size), true)
  })
  return b
}

describe('paged hierarchy (M05-FR-019)', () => {
  it('a 3-level paged hierarchy expands to the same tree as the single chunk', async () => {
    // single chunk: r -> r0, r3 ; r0 -> r00 ; r3 -> r31, r37 ; r31 -> r310
    const single = encode([
      { type: 0, mask: 0b1001, n: 10, off: 0, size: 120 }, { type: 0, mask: 0b1, n: 5, off: 120, size: 60 }, { type: 0, mask: 0b10000010, n: 6, off: 180, size: 72 },
      { type: 1, mask: 0, n: 3, off: 252, size: 36 }, { type: 0, mask: 0b1, n: 2, off: 288, size: 24 }, { type: 1, mask: 0, n: 4, off: 312, size: 48 },
      { type: 1, mask: 0, n: 1, off: 360, size: 12 },
    ])
    const ref = parseHierarchy(single)
    // paged: chunk A has r, r0 (proxy -> chunk B), r3 (proxy -> chunk C); chunk C has r3, r31 (proxy -> chunk D), r37
    const B = encode([{ type: 0, mask: 0b1, n: 5, off: 120, size: 60 }, { type: 1, mask: 0, n: 3, off: 252, size: 36 }])
    const D = encode([{ type: 0, mask: 0b1, n: 2, off: 288, size: 24 }, { type: 1, mask: 0, n: 1, off: 360, size: 12 }])
    const Cpre = { type: 0, mask: 0b10000010, n: 6, off: 180, size: 72 }
    const layout = (offB: number, offC: number, offD: number) => [
      encode([{ type: 0, mask: 0b1001, n: 10, off: 0, size: 120 }, { type: 2, mask: 0, n: 5, off: offB, size: B.byteLength }, { type: 2, mask: 0, n: 6, off: offC, size: 3 * HIER_RECORD }]),
      encode([Cpre, { type: 2, mask: 0, n: 2, off: offD, size: D.byteLength }, { type: 1, mask: 0, n: 4, off: 312, size: 48 }]),
    ]
    const [A0] = layout(0, 0, 0)
    const offB = A0.byteLength
    const offC = offB + B.byteLength
    const offD = offC + 3 * HIER_RECORD
    const [A, C] = layout(offB, offC, offD)
    const file = new Uint8Array(offD + D.byteLength)
    file.set(new Uint8Array(A), 0)
    file.set(new Uint8Array(B), offB)
    file.set(new Uint8Array(C), offC)
    file.set(new Uint8Array(D), offD)
    expect(parseChunk(file.buffer, 0, A.byteLength).some((r) => r.type === 2)).toBe(true)
    const paged = await parseHierarchyPaged(file.buffer.slice(0, A.byteLength), null, async (o, s) => file.buffer.slice(o, o + s), null)
    const key = (rs: typeof ref) => rs.map((r) => `${r.name}:${r.numPoints}:${r.byteOffset}:${r.byteSize}:${r.childMask}`).sort()
    expect(key(paged.records)).toEqual(key(ref))
    // parents precede children and names follow the child order
    const idx = new Map(paged.records.map((r, i) => [r.name, i]))
    for (const r of paged.records) if (r.parent >= 0) expect(idx.get(r.name)!).toBeGreaterThan(r.parent)
  })
})

describe.skipIf(!haveWorlds)('openWorld errors and forests', () => {
  it('a 409 during open is reported as 402 PC_CONTENT_STALE', async () => {
    const s = new WorldServer()
    s.cvOverride = 'x'
    const f = s.fetch
    const bad = (async (u: RequestInfo | URL, i?: RequestInit) => {
      const url = typeof u === 'string' ? u : u instanceof URL ? u.href : u.url
      // world.json advertises the real version, every ?v= request answers 409
      if (url.endsWith('world.json')) return new Response(readFileSync(new URL('shenzhen/world.json', WORLDS)), { status: 200 })
      return f(u, i)
    }) as typeof fetch
    const fetcher = new Fetcher({ workers: 1, limit: 4, useWorker: false, f: bad })
    await expect(openWorld('http://awr.test/worlds/shenzhen/', { fetcher, poolCapacityPts: 253_952, startHi: 40_000, now: () => 0, f: bad })).rejects.toMatchObject({ code: 402 })
    fetcher.dispose()
    s.close()
  })
  it('suzhou: one first-screen Range per root (6), at most 4 in flight, TTFP start at the last arrival', async () => {
    const s = new WorldServer(5)
    const fetcher = new Fetcher({ workers: 1, limit: 4, useWorker: false, f: s.fetch })
    const w = await openWorld('http://awr.test/worlds/suzhou/', { fetcher, poolCapacityPts: PC.poolWidth * PC.poolRowsS, startHi: LADDER[0].hi, now: () => 0, f: s.fetch })
    const ranges = s.log.filter((e) => e.range !== null)
    expect(ranges).toHaveLength(6)
    expect(w.firstScreenLevel).toBe(0)
    expect(w.firstScreenBytes).toBe(243_924)
    expect(s.maxInflight).toBeLessThanOrEqual(4)
    expect(w.store.roots.length).toBe(6)
    expect(w.ttfpStart).toBeGreaterThan(0)
    fetcher.dispose()
    s.close()
  })
})
