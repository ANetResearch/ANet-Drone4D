// Front-end tape decoder and walkable surface height against the Python golden (M23 §6.17 cross-language checklist;
// D3-AC-07; tools/contracts/gen_tape_golden.py).
import { describe, expect, it } from 'vitest'
import { TapeError, TapeReader, FIELDS, WALK_ABOVE_M, WalkMesh, walkableHeight, type WalkTile } from '../../src/engine/actors/tape'
import { readBytes, readJson, sameRaw, sha256Hex } from '../contracts/util'

interface Sample { t_ns: number; ids: number[] | null; pos_enu_m: number[][]; yaw_rad: number[]; vel_enu_mps: number[][]; state: number[]; container: number[] }
interface Golden { n_ticks: number; n_actors: number; n_scripted: number; hashes: Record<string, string>; records: Record<string, number[][]>; actor_states: Sample[] }

const g = readJson<Golden>('golden/event/tape-small.json')
const bin = readBytes('golden/event/tape-small.bin')

describe('tape decoder', () => {
  const r = new TapeReader(bin)

  it('parses the container and header', () => {
    expect([r.nTicks, r.nActors, r.nScripted, r.blocks.length]).toEqual([g.n_ticks, g.n_actors, g.n_scripted, 3])
    expect(r.header.event_id).toBe('golden-small')
    expect(sha256Hex(r.headerBytes)).toBe(g.hashes.header_sha256)
  })

  it('decodes every record like Python and reproduces the content hash', () => {
    for (const [name] of FIELDS) {
      const got = Array.from({ length: r.nActors }, (_, i) => Array.from({ length: r.nTicks }, (_, k) => r.value(name, i, k)))
      expect(got).toEqual(g.records[name])
    }
    const body = r.canonicalBody()
    expect(sha256Hex(body)).toBe(g.hashes.body_sha256)
    const all = new Uint8Array(r.headerBytes.byteLength + body.byteLength)
    all.set(r.headerBytes)
    all.set(body, r.headerBytes.byteLength)
    expect(sha256Hex(all)).toBe(g.hashes.content_sha256)
  })

  it.each(g.actor_states.map((s, i) => [i, s] as const))('actor_states sample %i matches Python bit for bit', (_i, s) => {
    const st = r.actorStates(s.t_ns, s.ids ?? undefined)
    const tri = (a: Float64Array): number[][] => Array.from({ length: a.length / 3 }, (_, j) => Array.from(a.subarray(3 * j, 3 * j + 3)))
    expect(tri(st.pos)).toEqual(s.pos_enu_m)
    expect(tri(st.vel)).toEqual(s.vel_enu_mps)
    expect(Array.from(st.yaw)).toEqual(s.yaw_rad)
    expect(Array.from(st.state)).toEqual(s.state)
    expect(Array.from(st.container)).toEqual(s.container)
  })

  it('only the scripted sub-blocks are decoded for scripted queries', () => {
    const fresh = new TapeReader(bin)
    fresh.actorStates(1_234_000_000, [0, 1])
    expect([...(fresh as unknown as { cache: Map<string, unknown> }).cache.keys()]).toEqual(['0S'])
  })

  it('rejects damaged input', () => {
    expect(() => new TapeReader(new Uint8Array(32))).toThrow(TapeError)
    const bad = bin.slice()
    bad[r.blocks[1].offS + 8] ^= 0xff
    expect(() => new TapeReader(bad).value('x_mm', 0, 150)).toThrow(TapeError)
  })
})

type Raw = number | 'NaN' | 'Infinity'
interface WalkGolden {
  above_m: number
  xyz_bytes: number
  tri_bytes: number
  tiles: WalkTile[]
  counts: { vertices: number; triangles: number }
  queries: { x: number[]; y: number[]; nav_z: Raw[]; z: Raw[]; z32: Raw[]; prim: number[] }
}

const num = (v: Raw): number => (v === 'NaN' ? NaN : v === 'Infinity' ? Infinity : v)

describe('walkable height (MeshWorldQuery.walkable_height)', () => {
  const w = readJson<WalkGolden>('golden/event/walkable-small.json')
  const raw = readBytes('golden/event/walkable-small.bin')
  const buf = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength)
  const mesh = new WalkMesh(new Float32Array(buf, 0, w.xyz_bytes / 4), new Uint32Array(buf, w.xyz_bytes, w.tri_bytes / 4), w.tiles)
  const q = w.queries
  const n = q.x.length
  const xy = Float64Array.from({ length: 2 * n }, (_, i) => (i % 2 === 0 ? q.x[i >> 1] : q.y[i >> 1]))
  const top = Float64Array.from(q.nav_z, (v) => num(v) + WALK_ABOVE_M)

  it('reads the golden mesh', () => {
    expect(WALK_ABOVE_M).toBe(w.above_m)
    expect([mesh.xyz.length / 3, mesh.tri.length / 3]).toEqual([w.counts.vertices, w.counts.triangles])
    expect(raw.byteLength).toBe(w.xyz_bytes + w.tri_bytes)
  })

  it('float64 heights and triangle numbers match Python bit for bit', () => {
    const { z, prim } = mesh.surfaceBatch(xy, top)
    const bad = Array.from(z).flatMap((v, i) => (sameRaw(v, q.z[i]) && prim[i] === q.prim[i] ? [] : [i]))
    expect(bad).toEqual([])
    expect(Array.from(z).filter(Number.isFinite).length).toBeGreaterThan(0.6 * n)
  })

  it('float32 walkable heights match Python bit for bit', () => {
    const got = walkableHeight(mesh, xy, Float64Array.from(q.nav_z, num))
    const bad = Array.from(got).flatMap((v, i) => (sameRaw(v, q.z32[i]) ? [] : [i]))
    expect(bad).toEqual([])
    const ix = q.nav_z.flatMap((v, i) => (v === 'Infinity' ? [i] : []))
    const topOnly = mesh.walkableHeight(Float64Array.from(ix.flatMap((i) => [q.x[i], q.y[i]])))
    expect(Array.from(topOnly).every((v, j) => sameRaw(v, q.z32[ix[j]]))).toBe(true)
    expect(ix.length).toBeGreaterThan(100)
  })

  it('does not depend on query order', () => {
    const order = Array.from({ length: n }, (_, i) => (i * 7919) % n)
    const rev = new WalkMesh(mesh.xyz, mesh.tri, [...w.tiles].reverse())
    for (const i of order) {
      const h = rev.surface(q.x[i], q.y[i], top[i])
      expect(sameRaw(h.z, q.z[i]) && h.prim === q.prim[i]).toBe(true)
    }
  })
})

