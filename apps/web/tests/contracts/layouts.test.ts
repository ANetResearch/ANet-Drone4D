// layouts.json accessors: TS byte round trip against the Python golden (D1-AC-13; AWR-17 §10.3; g04 §5.5 vectors)
import { describe, expect, it } from 'vitest'
import * as L from '@awr/contracts/layouts'
import { bytesToHex, hexToBytes, readBytes, readJson, sameRaw, stemOf } from './util'

interface Values {
  layout_id: number
  schema_hash: Record<string, string>
  rows: Record<string, { count: number; size: number; values: Record<string, unknown>[] }>
}
interface Vector {
  name: string; flight_state: number; state: number; sub: number; flags?: number; ctrl?: number; owner?: number
  locked?: boolean; native?: number; pose_src?: number; full64_hex?: string; lite32_hex?: string
}
type Rec = Record<string, unknown>
type Reader = (dv: DataView, off: number, out: Rec) => Rec
type Writer = (dv: DataView, off: number, v: Rec) => void

const V = readJson<Values>('golden/layouts/values.json')
const lib = L as unknown as Record<string, unknown>

describe('layouts: identity', () => {
  it('LAYOUT_ID and per-schema hashes equal the Python generator', () => {
    expect(L.LAYOUT_ID).toBe(V.layout_id)
    expect({ ...L.SCHEMA_HASH }).toEqual(V.schema_hash)
  })
})

describe.each(Object.keys(V.rows))('layout %s', (sn) => {
  const meta = V.rows[sn]
  const stem = stemOf(sn)
  const read = lib[`read${stem}`] as Reader
  const write = lib[`write${stem}`] as Writer
  const make = lib[`new${stem}`] as () => Rec

  it('accessors exist', () => {
    expect(typeof read).toBe('function')
    expect(typeof write).toBe('function')
  })

  it(`decodes golden rows and re-encodes all ${meta.count} records byte for byte`, () => {
    const raw = readBytes(`golden/layouts/${sn}.bin`)
    expect(raw.byteLength).toBe(meta.count * meta.size)
    const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength)
    const out = new Uint8Array(raw.byteLength)
    const dvo = new DataView(out.buffer)
    const rec = make()
    for (let i = 0; i < meta.count; i++) {
      read(dv, i * meta.size, rec)
      if (i < meta.values.length) {
        for (const [k, v] of Object.entries(meta.values[i])) {
          const got = Array.isArray(rec[k]) ? [...(rec[k] as number[])] : rec[k]
          if (!sameRaw(got, v)) throw new Error(`${sn}[${i}].${k}: ${String(got)} != ${JSON.stringify(v)}`)
        }
      }
      write(dvo, i * meta.size, rec)
    }
    expect(bytesToHex(out) === bytesToHex(raw)).toBe(true)
  })
})

describe('layouts: g04 state vectors', () => {
  const vecs = readJson<{ vectors: Vector[] }>('golden/layouts/vectors.json').vectors
  it.each(vecs.map((v) => [v.name, v] as const))('%s', (_n, v) => {
    expect(L.packFs(v.state, v.sub)).toBe(v.flight_state)
    if (v.ctrl === undefined) return
    expect(L.packCtrl(v.owner!, v.locked!, v.native!, v.pose_src!)).toBe(v.ctrl)
    expect([L.ctrlOwner(v.ctrl), L.ctrlLocked(v.ctrl) === 1, L.ctrlNative(v.ctrl), L.ctrlPose(v.ctrl)]).toEqual([v.owner, v.locked, v.native, v.pose_src])
    const full = hexToBytes(v.full64_hex!)
    const lite = hexToBytes(v.lite32_hex!)
    expect([L.fsOf(full, 0), L.subOf(full, 0)]).toEqual([v.state, v.sub])
    expect([L.fsOf(lite, 0), L.subOf(lite, 0)]).toEqual([v.state, v.sub])
    const f = L.readDroneState64(new DataView(full.buffer), 0, L.newDroneState64())
    const s = L.readSwarmLite32(new DataView(lite.buffer), 0, L.newSwarmLite32())
    expect([f.flight_state, f.flags, f.ctrl]).toEqual([v.flight_state, v.flags, v.ctrl])
    expect([s.flight_state, s.flags, s.ctrl]).toEqual([v.flight_state, v.flags, v.ctrl])
    expect(L.flagBit(f.flags, 0)).toBe(1)
  })

  it('decodeSwarmLite32Into matches the per-row reader', () => {
    const raw = readBytes('golden/layouts/awr.SwarmLite32.v1.bin')
    const n = 100
    const soa = L.newSwarmSoA(n)
    const dv = new DataView(raw.buffer, raw.byteOffset, n * 32)
    expect(L.decodeSwarmLite32Into(raw.buffer, raw.byteOffset, n, soa)).toBe(n)
    const rec = L.newSwarmLite32()
    const same = (a: number, b: number) => Object.is(a, b) || (Number.isNaN(a) && Number.isNaN(b))
    for (let i = 0; i < n; i++) {
      L.readSwarmLite32(dv, i * 32, rec)
      expect([soa.agentNo[i], soa.fs[i], soa.battery[i], soa.flags[i], soa.ctrl[i]]).toEqual([rec.agent_no, rec.flight_state, rec.battery_pct, rec.flags, rec.ctrl])
      for (let j = 0; j < 3; j++) {
        expect(same(soa.pos[i * 3 + j], rec.pos[j])).toBe(true)
        expect(soa.vel[i * 3 + j]).toBe(Math.fround(rec.vel_cms[j] * L.SL32_SCALE.vel_cms))
      }
      for (let j = 0; j < 4; j++) expect(soa.quat[i * 4 + j]).toBe(Math.fround(rec.q_snorm[j] * L.SL32_SCALE.q_snorm))
    }
  })
})
