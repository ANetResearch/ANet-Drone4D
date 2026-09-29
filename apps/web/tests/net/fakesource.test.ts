// FakeSource (M11-FR-098, M11-AC-040; D1-AC-35 TS part): replaying the four .awrrt fixtures through FakeSource and the
// rt.worker engine (RtHost) yields exactly the recorded bytes and, decoded into TelemetryFrame slots, the values of the
// payload golden; synthesis for N in {1, 200, 1000} produces control messages and payloads that validate against the
// contract schemas and BATCH records that decode with the generated accessors; goto closes (accepted, running,
// succeeded) with the vehicle at the target.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import Ajv2020 from 'ajv/dist/2020.js'
import { decode as mpDecode } from '@msgpack/msgpack'
import * as L from '@awr/contracts/layouts'
import { AWRT_S2C, AWRT_TEXT, OP_BATCH, OP_TIME, newRecordView, nextRecord, readAwrrt, readFrameHeader } from '@awr/contracts/frame'
import { newTimeView, readTime } from '@awr/contracts/time'
import { FakeSource } from '@/net/rt/FakeSource'
import { REGION, SLOT_ITEM, SF } from '@/net/rt/frame'
import { WS_OPEN } from '@/net/rt/transport'
import { canonical, contractsPath, readBytes, readJson, sha256Hex } from '../contracts/util'
import { ctrlOps, microtasks, rig } from './harness'

type Rec = Record<string, unknown>
const FIXTURES = ['smoke_n1', 'swarm_n200', 'swarm_n1000', 'time_epoch']

describe.each(FIXTURES)('replay %s', (name) => {
  it('delivers the server records byte for byte in order', async () => {
    const bytes = readBytes(`fixtures/rt/${name}.awrrt`)
    let t = 0
    const fake = new FakeSource({ fixture: bytes, autoTick: false, now: () => t })
    const got: (string | Uint8Array)[] = []
    fake.onmessage = (e) => got.push(typeof e.data === 'string' ? e.data : new Uint8Array(e.data))
    await microtasks()
    expect(fake.readyState).toBe(WS_OPEN)
    const want = readAwrrt(bytes).records.filter((r) => r.dir === AWRT_S2C)
    for (t = 0; !fake.replayDone; t += 5) {
      fake.tick()
      await microtasks()
    }
    await microtasks()
    expect(got.length).toBe(want.length)
    const td = new TextDecoder()
    for (let i = 0; i < want.length; i++) {
      if (want[i].kind === AWRT_TEXT) expect(got[i], `record ${i}`).toBe(td.decode(want[i].payload))
      else expect(sha256Hex(got[i] as Uint8Array), `record ${i}`).toBe(sha256Hex(want[i].payload))
    }
  })

  it('decodes through rt.worker slots to the payload golden', async () => {
    const gold = readJson<Rec>(`fixtures/payloads/${name}.json`)
    const golden = (gold.records as Rec[]).filter((r) => r.dir === 0)
    const schemaOf = new Map<number, string>()
    for (const g of golden) {
      const tx = g.text as Rec | undefined
      if (tx?.op === 'advertise') for (const c of tx.channels as Rec[]) schemaOf.set(c.id as number, c.schemaName as string)
    }
    const r = await rig({ fixture: readBytes(`fixtures/rt/${name}.awrrt`) })
    r.pull()
    const t0 = r.clock.t
    for (let guard = 0; !r.fake.replayDone && guard < 1000; guard++) {
      await r.step(5)
      r.pull()
    }
    await r.step(5)
    expect(r.clock.t - t0).toBeGreaterThan(0)
    // expected acceptance by the spec rule (AWR-17 §6.10): same-epoch BATCH pass, a new-epoch BATCH waits for the next
    // TIME and passes only when that TIME carries its epoch; old-epoch BATCH are dropped
    let cur = -1
    let stash: Rec | null = null
    const accepted: Rec[] = []
    let dropped = 0
    for (const g of golden) {
      if (g.op === 'TIME') {
        const tm = g.time as Rec
        cur = tm.epoch as number
        if (stash) {
          if ((stash.epoch as number) === cur) accepted.push(stash)
          else dropped++
          stash = null
        }
      } else if (g.op === 'BATCH') {
        const b = g.batch as Rec
        if (b.epoch === cur) accepted.push(b)
        else {
          if (stash) dropped++
          stash = b
        }
      }
    }
    const last = r.slots[r.slots.length - 1]
    expect(last.hdr.droppedEpochFrames).toBe(dropped)
    if (name === 'time_epoch') expect(dropped).toBe(1) // the stale epoch-1 BATCH after the seek TIME
    // every accepted swarm record appears in some slot with identical decoded rows
    const swarmSeqs = new Map<number, (typeof r.slots)[number]>()
    for (const s of r.slots) if (s.hdr.swarmN > 0) swarmSeqs.set(s.hdr.swarmSeq, s)
    let checked = 0
    for (const b of accepted) {
      for (const rec of b.records as Rec[]) {
        if (!('rows_head' in rec) || schemaOf.get(rec.channel_id as number) !== 'awr.SwarmLite32.v1') continue
        const slot = swarmSeqs.get(rec.seq as number)
        expect(slot, `swarm seq ${String(rec.seq)}`).toBeDefined()
        const sw = slot!.swarm
        expect(slot!.hdr.swarmN).toBe(rec.n_rows)
        const n = rec.n_rows as number
        const rows = [...(rec.rows_head as Rec[]).map((x, i) => [i, x] as const), ...(rec.rows_tail as Rec[]).map((x, i) => [n - (rec.rows_tail as Rec[]).length + i, x] as const)]
        for (const [i, row] of rows) {
          expect(sw.agentNo[i]).toBe(row.agent_no)
          expect(sw.fs[i]).toBe(row.flight_state)
          expect(sw.battery[i]).toBe(row.battery_pct)
          expect(sw.flags[i]).toBe(row.flags)
          expect(sw.ctrl[i]).toBe(row.ctrl)
          const pos = row.pos as number[]
          for (let j = 0; j < 3; j++) expect(sw.pos[3 * i + j]).toBe(Math.fround(pos[j]))
          const q = row.q_snorm as number[]
          for (let j = 0; j < 4; j++) expect(sw.quat[4 * i + j]).toBe(Math.fround(q[j] * L.SL32_SCALE.q_snorm))
          const v = row.vel_cms as number[]
          for (let j = 0; j < 3; j++) expect(sw.vel[3 * i + j]).toBe(Math.fround(v[j] * L.SL32_SCALE.vel_cms))
        }
        checked++
      }
    }
    expect(checked).toBeGreaterThan(0)
    // Full64 records: raw bytes copied into the slot decode to the golden row
    for (const b of accepted) {
      for (const rec of b.records as Rec[]) {
        if (!('rows_head' in rec) || schemaOf.get(rec.channel_id as number) !== 'awr.DroneState64.v1') continue
        const row = (rec.rows_head as Rec[])[0]
        const slot = r.slots.find((s) => {
          for (let k = 0; k < s.full.count; k++) if (s.full.bytes.getUint32(REGION.full + SLOT_ITEM * k + 4, true) === rec.seq) return true
          return false
        })
        expect(slot, `full seq ${String(rec.seq)}`).toBeDefined()
        let k = 0
        while (slot!.full.bytes.getUint32(REGION.full + SLOT_ITEM * k + 4, true) !== rec.seq) k++
        const d = L.readDroneState64(slot!.full.bytes, REGION.full + SLOT_ITEM * k + 16, L.newDroneState64())
        expect(d.agent_no).toBe(row.agent_no)
        expect(d.pos.map((x) => x)).toEqual((row.pos as number[]).map((x) => Math.fround(x)))
        expect(slot!.fullAgentNo(k)).toBe(row.agent_no)
      }
    }
    // roster arrives on the control path, identical to the golden decode
    const roster = ctrlOps(r.ctrl, 'roster')
    const gr = accepted.flatMap((b) => (b.records as Rec[]).filter((x) => x.channel_id === 1))
    if (gr.length) {
      expect(roster.length).toBeGreaterThan(0)
      const g0 = gr[0]
      const mine = { roster_version: roster[0].version, entries: roster[0].entries }
      if ('decoded' in g0) expect(mine).toEqual(g0.decoded)
      else expect(sha256Hex(canonical(mine))).toBe(g0.decoded_sha256)
    }
    // TIME state byte (bit 7 replay) is carried in the slot header
    if (name === 'time_epoch') {
      expect(r.slots.some((s) => (s.hdr.timeState & 0x80) !== 0 && (s.hdr.timeState & 0x0f) === 1)).toBe(true)
      expect(r.slots.some((s) => (s.hdr.flags & SF.EPOCH_CHANGED) !== 0 && s.hdr.timeEpoch === 0)).toBe(true)
    }
  })
})

// ---------------------------------------------------------------- synthesis against the contract schemas
const ajv = new Ajv2020({ strict: true, allErrors: true, strictTuples: false, strictRequired: false, allowUnionTypes: true })
ajv.addKeyword({ keyword: 'x-note' })
ajv.addKeyword({ keyword: 'unit' })
ajv.addFormat('date-time', /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$/)
const ids = new Map<string, string>()
const walk = (u: URL, rel: string): void => {
  for (const n of readdirSync(u)) {
    const p = new URL(n, u)
    if (statSync(p).isDirectory()) {
      if (!['gen', 'node_modules', 'fixtures', 'golden'].includes(n)) walk(new URL(`${n}/`, u), `${rel}${n}/`)
    } else if (n.endsWith('.schema.json')) {
      const s = JSON.parse(readFileSync(contractsPath(`${rel}${n}`), 'utf8')) as { $id: string }
      ids.set(`${rel}${n}`, s.$id)
      ajv.addSchema(s)
    }
  }
}
walk(contractsPath(''), '')
const validate = (schemaRel: string, data: unknown): string[] => {
  const v = ajv.getSchema(ids.get(schemaRel)!)!
  return v(data) ? [] : (v.errors ?? []).map((e) => `${e.instancePath} ${e.message}`)
}

describe.each([1, 200, 1000])('synthesis N = %i', (n) => {
  it('control messages, payloads and BATCH records follow the contracts', async () => {
    let t = 0
    const fake = new FakeSource({ n, autoTick: false, now: () => t, eventPeriodS: 0.25 })
    const text: Rec[] = []
    const bins: Uint8Array[] = []
    fake.onmessage = (e) => {
      if (typeof e.data === 'string') text.push(JSON.parse(e.data) as Rec)
      else bins.push(new Uint8Array(e.data))
    }
    await microtasks()
    fake.send(JSON.stringify({ op: 'hello', client: 'test', contracts: L.CONTRACTS_VERSION, tier: 'S' }))
    fake.send(JSON.stringify({ op: 'subscribe', subs: [
      { id: 1, topic: 'fleet/roster', rate: 10, mode: 'latest' }, { id: 2, topic: 'swarm/state', rate: 20, mode: 'latest' },
      { id: 3, topic: 'env/state', rate: 10, mode: 'latest' }, { id: 4, topic: 'event', rate: 0, mode: 'all' },
      { id: 5, topic: `uav/${fake.ids[0]}/state`, rate: 60, mode: 'latest' }, { id: 6, topic: `uav/${fake.ids[0]}/state_ext`, rate: 2, mode: 'latest' },
      { id: 7, topic: 'perf/server', rate: 1, mode: 'latest' }, { id: 8, topic: 'sys/procs', rate: 1, mode: 'latest' }] }))
    fake.send(JSON.stringify({ op: 'ping', t: 5 }))
    let acked = 0
    for (let k = 0; k < 90; k++) {
      t += 1000 / 60
      fake.tick()
      await microtasks()
      if (bins.length) {
        const last = bins[bins.length - 1]
        if (last[0] === OP_BATCH) {
          const seq = new DataView(last.buffer, last.byteOffset).getUint32(4, true)
          if (seq > acked) {
            acked = seq
            fake.send(JSON.stringify({ op: 'ack', frame: seq }))
          }
        }
      }
    }
    // control messages against rt/ops.schema.json
    expect(text.map((m) => m.op)).toContain('serverInfo')
    for (const m of text) expect(validate('rt/ops.schema.json', m), JSON.stringify(m).slice(0, 200)).toEqual([])
    expect(text.some((m) => m.op === 'event' || m.op === 'events')).toBe(true)
    expect(text.find((m) => m.op === 'pong')).toBeDefined()
    // binary frames
    const td = new TextDecoder()
    void td
    const adv = text.find((m) => m.op === 'advertise')!.channels as Rec[]
    const schemaOf = new Map(adv.map((c) => [c.id as number, c.schemaName as string]))
    let swarm = 0
    let full = 0
    let msgpack = 0
    let snapshot = false
    let lastSeq = 0
    for (const b of bins) {
      const dv = new DataView(b.buffer, b.byteOffset, b.byteLength)
      if (b[0] === OP_TIME) {
        const tv = readTime(dv, 0, newTimeView())
        expect(tv.state).toBe(9)
        continue
      }
      expect(b[0]).toBe(OP_BATCH)
      const h = readFrameHeader(dv, L.newRtBatchHeader())
      expect(h.frame_seq).toBe(lastSeq + 1)
      lastSeq = h.frame_seq
      if (h.flags & 1) snapshot = true
      const rv = newRecordView()
      let prevPrio = -1
      for (let off = 16; off >= 0 && off < b.byteLength; ) {
        off = nextRecord(dv, off, rv)
        expect(rv.payload_off % 8).toBe(0)
        const sn = schemaOf.get(rv.channel_id)!
        const prio = adv.find((c) => c.id === rv.channel_id)!.priority as number
        if (sn !== 'awr.fleet.roster.v1') {
          expect(prio).toBeGreaterThanOrEqual(prevPrio)
          prevPrio = prio
        }
        const body = b.subarray(rv.payload_off, rv.payload_off + rv.length)
        if (sn === 'awr.SwarmLite32.v1') {
          expect(rv.length).toBe(32 * n)
          const soa = L.newSwarmSoA(n)
          L.decodeSwarmLite32Into(body.slice().buffer, 0, n, soa)
          for (let i = 0; i < n; i++) expect(soa.agentNo[i]).toBe(i)
          expect(soa.fs[0] & 0x1f).toBe(5)
          const q = soa.quat
          expect(Math.hypot(q[0], q[1], q[2], q[3])).toBeCloseTo(1, 3)
          swarm++
        } else if (sn === 'awr.DroneState64.v1') {
          expect(rv.length).toBe(64)
          const d = L.readDroneState64(new DataView(body.slice().buffer), 0, L.newDroneState64())
          expect(d.agent_no).toBe(0)
          expect(d.mission_item).toBe(0xffff)
          full++
        } else {
          const obj = mpDecode(body)
          const schema = { 'awr.fleet.roster.v1': 'rt/payloads/fleet_roster.schema.json', 'awr.env.keyframe.v1': 'env/env_state.schema.json',
            'awr.uav.state_ext.v1': 'rt/payloads/uav_state_ext.schema.json', 'awr.perf.server.v1': 'rt/payloads/perf_server.schema.json',
            'awr.sys.procs.v1': 'rt/payloads/sys_procs.schema.json' }[sn]
          expect(schema, sn).toBeDefined()
          expect(validate(schema!, obj), sn).toEqual([])
          msgpack++
        }
      }
    }
    expect(snapshot).toBe(true)
    expect(swarm).toBeGreaterThanOrEqual(10) // 20 Hz over 1.5 s with the credit window respected by our acks
    expect(full).toBeGreaterThanOrEqual(10)
    expect(msgpack).toBeGreaterThanOrEqual(4)
    expect(fake.stats.creditSkips).toBe(0)
  })
})

describe('credit window', () => {
  it('stops sending BATCH when frame_seq - acked reaches W and resumes after an ack', async () => {
    let t = 0
    const fake = new FakeSource({ n: 1, window: 4, autoTick: false, now: () => t })
    const bins: Uint8Array[] = []
    fake.onmessage = (e) => {
      if (typeof e.data !== 'string' && new Uint8Array(e.data)[0] === OP_BATCH) bins.push(new Uint8Array(e.data))
    }
    await microtasks()
    fake.send(JSON.stringify({ op: 'hello', client: 'test', contracts: L.CONTRACTS_VERSION }))
    fake.send(JSON.stringify({ op: 'subscribe', subs: [{ id: 1, topic: 'swarm/uav/state', rate: 60, mode: 'latest' }] }))
    for (let k = 0; k < 20; k++) {
      t += 1000 / 60
      fake.tick()
      await microtasks()
    }
    expect(bins.length).toBe(4)
    expect(fake.stats.creditSkips).toBeGreaterThan(0)
    fake.send(JSON.stringify({ op: 'ack', frame: 4 }))
    t += 1000 / 60
    fake.tick()
    await microtasks()
    expect(bins.length).toBe(5)
  })
})

describe('goto through the rt.worker engine', () => {
  it('accepted, running, progress, succeeded; the vehicle stops at the target', async () => {
    const r = await rig({ url: 'fake:?n=1', start: [0, 0, 100] })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 10, mode: 'latest' }, { id: 2, topic: 'fleet/roster', rate: 10, mode: 'latest' }] })
    r.pull()
    for (let k = 0; k < 10; k++) {
      await r.step(1000 / 60)
      r.pull()
    }
    expect(r.host.connState).toBe('LIVE')
    r.host.onMessage({ cmd: 'call', id: 'c-1', service: 'uav/p600-01/cmd/goto', args: { pos: [30, -20, 110], speed_mps: 12 }, timeoutMs: 3000 })
    for (let k = 0; k < 60 * 8; k++) {
      await r.step(1000 / 60)
      r.pull()
      if (ctrlOps(r.ctrl, 'result').some((m) => m.status === 'succeeded')) break
    }
    const results = ctrlOps(r.ctrl, 'result').filter((m) => m.id === 'c-1')
    expect(results.map((m) => m.status)).toEqual(['accepted', 'running', 'succeeded'])
    expect(ctrlOps(r.ctrl, 'progress').length).toBeGreaterThan(0)
    const fin = results[2]
    expect(fin.final).toBe(true)
    expect((fin.effect as Rec).simulated).toBe(true)
    const p = r.fake.positionOf(0)
    expect(Math.hypot(p[0] - 30, p[1] + 20, p[2] - 110)).toBeLessThanOrEqual(0.5)
    // a goto to an unknown vehicle is rejected by the admission stand-in
    r.host.onMessage({ cmd: 'call', id: 'c-2', service: 'uav/nope/cmd/goto', args: { pos: [0, 0, 50] }, timeoutMs: 3000 })
    await r.step(20)
    r.pull()
    await r.step(20)
    const rej = ctrlOps(r.ctrl, 'result').find((m) => m.id === 'c-2')!
    expect([rej.status, rej.code]).toEqual(['rejected', 107])
  })
})
