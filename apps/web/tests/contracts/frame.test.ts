// BATCH golden and .awrrt protocol fixtures decoded by the TS reference path (AWR-17 §6.4; AWR-16 §13.9 DATA-AC-018; D1-AC-35)
import { describe, expect, it } from 'vitest'
import { decode as mpDecode } from '@msgpack/msgpack'
import * as L from '@awr/contracts/layouts'
import { ENC_JSON, ENC_MSGPACK, ENC_RAW, OP_BATCH, OP_TIME, newRecordView, nextRecord, readAwrrt, readFrameHeader, AWRT_TEXT } from '@awr/contracts/frame'
import { advances, newTimeView, readTime } from '@awr/contracts/time'
import { bytesToHex, canonical, hexToBytes, readBytes, readJson, sha256Hex, stemOf } from './util'

type Rec = Record<string, unknown>
type Reader = (dv: DataView, off: number, out: Rec) => Rec
const lib = L as unknown as Record<string, unknown>

interface GoldRecord { channel_id: number; encoding: number; rflags: number; length: number; seq: number; dt_us: number; payload_off: number; payload_hex: string }
interface GoldFrame { hex: string; flags: number; epoch: number; frame_seq: number; t_sim_ns: number; records: GoldRecord[] }

describe('BATCH golden', () => {
  const frames = readJson<{ frames: GoldFrame[] }>('golden/rt/batch.json').frames
  it.each(frames.map((f, i) => [i, f] as const))('frame %i walks like Python', (_i, f) => {
    const b = hexToBytes(f.hex)
    const dv = new DataView(b.buffer)
    const h = readFrameHeader(dv, L.newRtBatchHeader())
    expect([h.flags, h.epoch, h.frame_seq, h.t_sim_ns]).toEqual([f.flags, f.epoch, f.frame_seq, f.t_sim_ns])
    const r = newRecordView()
    const got: GoldRecord[] = []
    for (let off = nextRecord(dv, 16, r); ; off = nextRecord(dv, off, r)) {
      got.push({ ...r, payload_hex: bytesToHex(b.subarray(r.payload_off, r.payload_off + r.length)) })
      if (off < 0 || off >= b.byteLength) break
    }
    expect(got).toEqual(f.records)
  })

  it('truncated frames throw instead of reading out of range', () => {
    const b = hexToBytes(frames[0].hex)
    const cut = b.subarray(0, 40)
    const dv = new DataView(cut.buffer, cut.byteOffset, cut.byteLength)
    expect(() => nextRecord(dv, 16, newRecordView())).toThrow()
  })
})

// ---------------------------------------------------------------- .awrrt fixtures
const FIXTURES = ['smoke_n1', 'swarm_n200', 'swarm_n1000', 'time_epoch']
const td = new TextDecoder()

const jv = (x: unknown): unknown => {
  if (typeof x === 'number') return Number.isNaN(x) ? 'NaN' : Object.is(x, -0) ? '-0' : x
  if (Array.isArray(x)) return x.map(jv)
  return x
}

function rowsOf(schemaName: string, body: Uint8Array, from: number, to: number): Rec[] {
  const read = lib[`read${stemOf(schemaName)}`] as Reader
  const make = lib[`new${stemOf(schemaName)}`] as () => Rec
  const size = L.SIZES[schemaName]
  const dv = new DataView(body.buffer, body.byteOffset, body.byteLength)
  const out: Rec[] = []
  for (let i = from; i < to; i++) {
    const r = read(dv, i * size, make())
    out.push(Object.fromEntries(Object.entries(r).map(([k, v]) => [k, jv(Array.isArray(v) ? [...v] : v)])))
  }
  return out
}

function decodePayload(ch: Rec | undefined, enc: number, body: Uint8Array): Rec {
  if (!ch) return { unknown_channel: true }
  const sn = ch.schemaName as string
  const sizes = L.SIZES
  if (enc === ENC_RAW && sn in sizes) {
    const n = body.byteLength / sizes[sn]
    return { n_rows: n, rows_head: rowsOf(sn, body, 0, Math.min(4, n)), rows_tail: n > 4 ? rowsOf(sn, body, n - 4, n) : [] }
  }
  if (enc === ENC_MSGPACK || enc === ENC_JSON) {
    const obj = enc === ENC_MSGPACK ? mpDecode(body) : JSON.parse(td.decode(body))
    return body.byteLength <= 4096 ? { decoded: obj } : { decoded_sha256: sha256Hex(canonical(obj)) }
  }
  return {}
}

function decodeFixture(data: Uint8Array): Rec {
  const f = readAwrrt(data)
  const channels = new Map<number, Rec>()
  const records = f.records.map((r) => {
    const d: Rec = { dir: r.dir, kind: r.kind, t_rel_ns: r.t_rel_ns, len: r.payload.byteLength, payload_sha256: sha256Hex(r.payload) }
    if (r.kind === AWRT_TEXT) {
      const msg = JSON.parse(td.decode(r.payload)) as Rec
      d.text = msg
      if (msg.op === 'advertise') for (const c of msg.channels as Rec[]) channels.set(c.id as number, c)
      return d
    }
    const dv = new DataView(r.payload.buffer, r.payload.byteOffset, r.payload.byteLength)
    const op = r.payload[0]
    if (op === OP_TIME) {
      const t = readTime(dv, 0, newTimeView())
      d.op = 'TIME'
      d.time = { state: t.state, replay: t.replay, epoch: t.epoch, rate: t.rate, t_sim_ns: t.t_sim_ns, t_srv_ns: t.t_srv_ns, state_byte: r.payload[1], advances: advances(r.payload[1]) }
    } else if (op === OP_BATCH) {
      const h = readFrameHeader(dv, L.newRtBatchHeader())
      const rv = newRecordView()
      const recs: Rec[] = []
      for (let off = 16; off >= 0 && off < r.payload.byteLength; ) {
        off = nextRecord(dv, off, rv)
        const body = r.payload.subarray(rv.payload_off, rv.payload_off + rv.length)
        recs.push({ channel_id: rv.channel_id, encoding: rv.encoding, rflags: rv.rflags, length: rv.length, seq: rv.seq, dt_us: rv.dt_us,
          ...decodePayload(channels.get(rv.channel_id), rv.encoding, body) })
      }
      d.op = 'BATCH'
      d.batch = { flags: h.flags, epoch: h.epoch, frame_seq: h.frame_seq, t_sim_ns: h.t_sim_ns, records: recs }
    } else d.op = `0x${op.toString(16).padStart(2, '0')}`
    return d
  })
  return { flags: f.flags, t0_wall_ns: Number(f.t0_wall_ns), header: f.header, records }
}

describe.each(FIXTURES)('.awrrt fixture %s', (name) => {
  it('decodes to the payload golden', () => {
    const data = readBytes(`fixtures/rt/${name}.awrrt`)
    const gold = readJson<Rec>(`fixtures/payloads/${name}.json`)
    expect(sha256Hex(data)).toBe(gold.sha256)
    const hdr = gold.header as Rec
    expect(parseInt(hdr.layout_id as string, 16)).toBe(L.LAYOUT_ID)
    const dec = decodeFixture(data)
    expect(dec.header).toEqual(gold.header)
    expect(dec.flags).toBe(gold.flags)
    const gr = gold.records as Rec[]
    const dr = dec.records as Rec[]
    expect(dr.length).toBe(gr.length)
    for (let i = 0; i < gr.length; i++) expect(dr[i], `record ${i}`).toEqual(gr[i])
  })
})
