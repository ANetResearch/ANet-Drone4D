// Front-end tape decoder against the Python golden (M23 §6.17 cross-language checklist; tools/contracts/gen_tape_golden.py).
import { describe, expect, it } from 'vitest'
import { TapeError, TapeReader, FIELDS } from '../../src/engine/actors/tape'
import { readBytes, readJson, sha256Hex } from '../contracts/util'

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
