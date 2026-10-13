// D2 contracts 1.1.0 on the TS side (WP-01; AWR-17 §17.7, §17.9, §17.11; API-AC-055): TargetLite32 golden vectors, enum appends,
// reasons 500-599, D2 topics, the D2 sandbox fixture and the closed-form TTPF cases shared with Python.
import { describe, expect, it } from 'vitest'
import { decode as mpDecode } from '@msgpack/msgpack'
import * as L from '@awr/contracts/layouts'
import * as E from '@awr/contracts/enums'
import { REASONS, WS_CLOSE } from '@awr/contracts/reasons'
import { matchTopic } from '@awr/contracts/topics'
import { matchService } from '@awr/contracts/commands'
import { AWRT_TEXT, ENC_MSGPACK, ENC_RAW, OP_BATCH, newRecordView, nextRecord, readAwrrt } from '@awr/contracts/frame'
import { hexToBytes, readBytes, readJson, sha256Hex } from './util'

interface TargetVector { name: string; class: number; state: number; best: number; flags: number; hex: string }
type Rec = Record<string, unknown>

const bits = (v: number, start: number, width: number): number => (v >>> start) & ((1 << width) - 1)

describe('awr.TargetLite32.v1', () => {
  const doc = readJson<{ schema_hash: string; vectors: TargetVector[] }>('golden/layouts/target_vectors.json')

  it('size and schema hash', () => {
    expect(L.TARGET_LITE32.SIZE).toBe(32)
    expect(L.SIZES['awr.TargetLite32.v1']).toBe(32)
    expect(L.SCHEMA_HASH['awr.TargetLite32.v1']).toBe(doc.schema_hash)
  })

  it.each(doc.vectors.map((v) => [v.name, v] as const))('golden vector %s', (_name, v) => {
    const b = hexToBytes(v.hex)
    const r = L.readTargetLite32(new DataView(b.buffer), 0, L.newTargetLite32())
    expect([r.class, r.state, r.best, r.flags]).toEqual([v.class, v.state, v.best, v.flags])
    const out = new Uint8Array(32)
    L.writeTargetLite32(new DataView(out.buffer), 0, r)
    expect(Array.from(out)).toEqual(Array.from(b))
  })

  it('bit fields of the dog vector (REACTING, physical, moving, occupied, sound on, enabled; LWIR D)', () => {
    expect(bits(0xfa, 0, 3)).toBe(E.Awareness.REACTING)
    expect(bits(0xfa, 3, 1)).toBe(E.CriterionMode.PHYSICAL)
    expect([bits(0xfa, 4, 1), bits(0xfa, 5, 1), bits(0xfa, 6, 1), bits(0xfa, 7, 1)]).toEqual([1, 1, 1, 1])
    expect(bits(0x31, 0, 2)).toBe(E.PerceptionLevel.D)
    expect(bits(0x31, 4, 4)).toBe(E.PerceptionSensor.LWIR)
  })
})

describe('enums, reasons, topics and commands (append only)', () => {
  it('enum appends keep D1 values', () => {
    expect(E.SensorKind.ILLUMINATOR).toBe(7)
    expect(E.SensorKind.ACOUSTIC).toBe(8)
    expect(E.SensorKind.BARO).toBe(6)
    expect(E.Kind.TARGET).toBe(6)
    expect(E.SetpointFrame.FW).toBe(2)
    expect(E.TargetClass.DOG).toBe(4)
  })

  it('reasons 500-599 and close code 4410', () => {
    const d2 = Object.values(REASONS).filter((r) => r.code >= 500 && r.code <= 599)
    expect(d2.length).toBe(43) // D2.1 契约 1.2.0 增加 515 至 519、564（17 §17.20.7；D2-INT）
    expect(REASONS[503].http).toBe(403)
    expect(REASONS[521].name).toBe('VEHICLE_ENV_LIMIT')
    expect(REASONS[522].name).toBe('VEHICLE_CLONE_INVALID')
    expect(WS_CLOSE.SANDBOX_GONE).toBe(4410)
  })

  it('D2 topics and commands resolve', () => {
    for (const t of ['swarm/target/state', 'target/person-01/detail', 'uav/p600-02/perception', 'uav/p600-02/sensor/radar/tracks', 'nest/nest-01/status',
      'task/task-01/status', 'relay/task-02/stats', 'sandbox/session']) expect(matchTopic(t), t).not.toBeNull()
    expect(matchTopic('swarm/target/state')?.schemaName).toBe('awr.TargetLite32.v1')
    expect(matchService('uav/p600-02/cmd/sensor/mode')?.service.op).toBe('sensor/mode')
    expect(matchService('task/task-01/cancel')?.service.op).toBe('task/cancel')
  })
})

describe('D2 sandbox fixture', () => {
  it('replays sandbox handshake, TargetLite32 rows and msgpack payloads', () => {
    const data = readBytes('fixtures/d2/sandbox_t5.awrrt')
    const gold = readJson<Rec>('fixtures/d2/sandbox_t5.json')
    expect(sha256Hex(data)).toBe(gold.sha256)
    const f = readAwrrt(data)
    expect(parseInt(f.header.layout_id as string, 16)).toBe(L.LAYOUT_ID)
    let channels = new Map<number, Rec>()
    let rows = 0
    let msgpacks = 0
    for (const r of f.records) {
      if (r.kind === AWRT_TEXT) {
        const m = JSON.parse(new TextDecoder().decode(r.payload)) as Rec
        if (m.op === 'serverInfo' && r.dir === 0) expect((m.sandbox as Rec).state).toBe('ACTIVE')
        if (m.op === 'advertise') channels = new Map((m.channels as Rec[]).map((c) => [c.id as number, c]))
        continue
      }
      if (r.payload[0] !== OP_BATCH) continue
      const dv = new DataView(r.payload.buffer, r.payload.byteOffset, r.payload.byteLength)
      const rv = newRecordView()
      for (let off = 16; off >= 0 && off < r.payload.byteLength;) {
        off = nextRecord(dv, off, rv)
        const ch = channels.get(rv.channel_id) as Rec
        if (rv.encoding === ENC_RAW && ch.schemaName === 'awr.TargetLite32.v1') {
          expect(rv.length % 32).toBe(0)
          const t = L.readTargetLite32(dv, rv.payload_off, L.newTargetLite32())
          expect([E.TargetClass.PERSON, E.TargetClass.DOG]).toContain(t.class)
          rows++
        } else if (rv.encoding === ENC_MSGPACK) {
          const obj = mpDecode(r.payload.subarray(rv.payload_off, rv.payload_off + rv.length)) as Rec
          expect(typeof obj).toBe('object')
          msgpacks++
        }
      }
    }
    expect(rows).toBe(40)
    expect(msgpacks).toBeGreaterThan(50)
  })
})

describe('closed-form cases shared with Python (M18-M19 PRD §7.6)', () => {
  it('TTPF P(N) = x^E / (1 + x^E), E = 2.7 + 0.7 x', () => {
    const cases = readJson<{ cases: { n: number; n50_px: number; p: number }[] }>('fixtures/perception/ttpf_cases.json').cases
    for (const c of cases) {
      const x = c.n / c.n50_px
      const xe = Math.pow(x, 2.7 + 0.7 * x)
      expect(Math.abs(xe / (1 + xe) - c.p)).toBeLessThan(1e-9)
    }
  })

  it('critical dimension of the class templates', () => {
    const cases = readJson<{ cases: { size_m: number[]; eps_deg: number; beta_deg: number; d_c_m: number }[] }>('fixtures/targets/dc_cases.json').cases
    for (const c of cases) {
      const [l, w, h] = c.size_m
      const e = (c.eps_deg * Math.PI) / 180
      const b = (c.beta_deg * Math.PI) / 180
      const d = Math.sqrt(Math.abs(Math.sin(e)) * l * w + Math.abs(Math.cos(e)) * (Math.abs(Math.cos(b)) * w * h + Math.abs(Math.sin(b)) * l * h))
      expect(Math.abs(d - c.d_c_m)).toBeLessThan(1e-9)
    }
  })
})
