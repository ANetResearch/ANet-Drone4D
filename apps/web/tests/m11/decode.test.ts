// rt.worker record indexing and slot decoding (M11-FR-089, FR-090, M11-AC-036): Lite32 N = 1000 into the SoA with the
// generated scales, Full64/EnvSample32/SensorPose48 copied as-is with agent and sample time, latest record per channel,
// unknown channels and encodings skipped by length, RESET channel ids, roster on the control path, and 10,000 fuzzed
// frames parsed without throwing (malformed frames counted, decoding continues).
import { describe, expect, it } from 'vitest'
import { encode as mpEncode } from '@msgpack/msgpack'
import * as L from '@awr/contracts/layouts'
import { BATCH_SNAPSHOT, ENC_MSGPACK, ENC_RAW, RF_RESET } from '@awr/contracts/frame'
import { RAW_SCHEMA, SF } from '@/net/rt/frame'
import { rng } from '../net/harness'
import { CH, batchFrame, lastFrame, full64, handshake, lite32, scriptRig, timeFrame } from './wire'


describe('decode into the slot', () => {
  it('Lite32 N = 1000 lands in the SoA with the generated scales', () => {
    const r = scriptRig()
    handshake(r)
    const rows = lite32(1000, (a) => ({ pos: [a * 0.5, -a, 100 + a], q_snorm: [0, 0, 16384, 28377], vel_cms: [a % 300, -(a % 200), 7], battery_pct: a % 101,
      flight_state: L.packFs(a % 14, a % 8), flags: a & 0xff, ctrl: (a * 7) & 0xff }))
    r.sock.serverSend(batchFrame({ flags: BATCH_SNAPSHOT, epoch: 1, frameSeq: 1, tSimNs: 5e9, records: [{ ch: CH.swarm, seq: 11, dtUs: -2000, payload: rows }] }))
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.swarmN).toBe(1000)
    expect(f.hdr.swarmSeq).toBe(11)
    expect(f.hdr.swarmTSimMs).toBeCloseTo(5000 - 2, 9)
    expect(f.hdr.frameSeqMax).toBe(1)
    expect(f.hdr.flags & SF.SNAPSHOT).toBeTruthy()
    for (const a of [0, 1, 499, 999]) {
      expect(f.swarm.agentNo[a]).toBe(a)
      expect(f.swarm.fs[a]).toBe(L.packFs(a % 14, a % 8))
      expect(f.swarm.battery[a]).toBe(a % 101)
      expect(f.swarm.flags[a]).toBe(a & 0xff)
      expect(f.swarm.ctrl[a]).toBe((a * 7) & 0xff)
      expect(f.swarm.pos[3 * a]).toBe(Math.fround(a * 0.5))
      expect(f.swarm.pos[3 * a + 2]).toBe(Math.fround(100 + a))
      expect(f.swarm.quat[4 * a + 2]).toBe(Math.fround(16384 * L.SL32_SCALE.q_snorm))
      expect(f.swarm.vel[3 * a]).toBe(Math.fround((a % 300) * L.SL32_SCALE.vel_cms))
      expect(f.swarm.vel[3 * a + 1] + 0).toBe(Math.fround(-(a % 200) * L.SL32_SCALE.vel_cms) + 0)
    }
    expect(r.host.connState).toBe('LIVE') // no subscriptions: the first TIME already made it LIVE
  })

  it('Full64, EnvSample32 and SensorPose48 are copied with agent, channel, seq and sample time', () => {
    const r = scriptRig()
    handshake(r)
    // roster maps the env channel of u1 to agent 1
    r.sock.serverSend(batchFrame({ epoch: 1, frameSeq: 1, tSimNs: 1e9, records: [{ ch: CH.roster, enc: ENC_MSGPACK, seq: 1,
      payload: mpEncode({ roster_version: 3, entries: [{ agent_no: 0, id: 'u0' }, { agent_no: 1, id: 'u1' }] }) }] }))
    r.pull()
    const env = new Uint8Array(32).map((_, i) => 100 + i)
    const pose = new Uint8Array(48)
    new DataView(pose.buffer).setUint16(0, 0, true)
    pose[2] = 4
    r.sock.serverSend(batchFrame({ epoch: 1, frameSeq: 2, tSimNs: 2e9, records: [
      { ch: CH.full0, seq: 5, dtUs: 1500, payload: full64(0, [1, 2, 3]) },
      { ch: CH.full1, seq: 6, payload: full64(1, [4, 5, 6]) },
      { ch: CH.envSample, seq: 7, payload: env },
      { ch: CH.pose, seq: 8, payload: pose },
      { ch: CH.unknownEnc, enc: 3, seq: 9, payload: new Uint8Array(13) }, // blob channel: skipped by length
      { ch: 999, seq: 1, payload: new Uint8Array(20) }, // not advertised: skipped
    ] }))
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.fullCount).toBe(2)
    expect(f.hdr.rawCount).toBe(2)
    expect(f.hdr.flags & SF.ROSTER_CHANGED).toBeFalsy() // the roster was in the previous slot
    const d0 = L.readDroneState64(f.dv, f.fullPayloadOff(0), L.newDroneState64())
    expect([f.fullAgentNo(0), f.fullChannelId(0), d0.pos]).toEqual([0, CH.full0, [1, 2, 3]])
    expect(f.fullTSampleMs(0)).toBeCloseTo(2001.5, 9)
    expect(f.dv.getUint32(f.fullPayloadOff(1) - 12, true)).toBe(6)
    expect(f.rawSchema(0)).toBe(RAW_SCHEMA.ENV_SAMPLE32)
    expect(f.rawAgentNo(0)).toBe(1)
    expect(f.dv.getUint8(f.rawPayloadOff(0) + 31)).toBe(131)
    expect(f.rawSchema(1)).toBe(RAW_SCHEMA.SENSOR_POSE48)
    expect(f.rawAgentNo(1)).toBe(0)
    const roster = r.ctrl.find((m) => m.op === 'roster')!
    expect(roster.version).toBe(3)
  })

  it('keeps only the latest record per channel between pulls; frameSeqMax is the highest frame', () => {
    const r = scriptRig()
    handshake(r)
    r.pull()
    for (let k = 1; k <= 4; k++) {
      r.sock.serverSend(batchFrame({ epoch: 1, frameSeq: k, tSimNs: k * 1e8, records: [{ ch: CH.swarm, seq: 100 + k, payload: lite32(2, () => ({ pos: [k, 0, 0] })) }] }))
    }
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.frameSeqMax).toBe(4)
    expect(f.hdr.swarmSeq).toBe(104)
    expect(f.swarm.pos[0]).toBe(4)
  })

  it('reports RESET channel ids once per slot', () => {
    const r = scriptRig()
    handshake(r)
    r.pull()
    r.sock.serverSend(batchFrame({ epoch: 1, frameSeq: 1, tSimNs: 1e9, records: [
      { ch: CH.full0, rflags: RF_RESET, seq: 1, payload: full64(0, [0, 0, 0]) }, { ch: CH.full1, seq: 1, payload: full64(1, [0, 0, 0]) }] }))
    r.sock.serverSend(batchFrame({ epoch: 1, frameSeq: 2, tSimNs: 1e9, records: [{ ch: CH.full0, rflags: RF_RESET, seq: 2, payload: full64(0, [0, 0, 0]) }] }))
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.resetCount).toBe(1)
    expect(f.resetChannelIds[0]).toBe(CH.full0)
    r.sock.serverSend(batchFrame({ epoch: 1, frameSeq: 3, tSimNs: 1e9, records: [{ ch: CH.full1, seq: 2, payload: full64(1, [0, 0, 0]) }] }))
    r.pull()
    const g = lastFrame(r)
    expect(g.hdr.resetCount).toBe(0)
    expect(g.resetChannelIds[0]).toBe(0)
  })
})

describe('fuzz (M11-AC-036: 10,000 frames without going out of bounds)', () => {
  it('random BATCH, TIME and garbage never throw; valid frames still decode afterwards', () => {
    const r = scriptRig()
    handshake(r)
    r.pull()
    const rand = rng(0x5eed)
    const chans = [CH.roster, CH.swarm, CH.env, CH.full0, CH.full1, CH.envSample, CH.pose, CH.unknownEnc, 7, 60000]
    let batches = 0
    for (let k = 0; k < 10_000; k++) {
      const kind = rand()
      let buf: ArrayBuffer
      if (kind < 0.1) {
        buf = new ArrayBuffer(Math.floor(rand() * 40))
        new Uint8Array(buf).forEach((_, i, a) => { a[i] = Math.floor(rand() * 256) })
      } else if (kind < 0.2) {
        buf = timeFrame({ epoch: 1, tSimNs: Math.floor(rand() * 1e12), tSrvNs: Math.floor(rand() * 1e12), state: Math.floor(rand() * 256) & 0x8f })
        if (rand() < 0.3) buf = buf.slice(0, Math.floor(rand() * 24))
      } else {
        const n = Math.floor(rand() * 5)
        const records = Array.from({ length: n }, () => {
          const len = Math.floor(rand() * 300)
          const payload = new Uint8Array(len)
          for (let i = 0; i < len; i++) payload[i] = Math.floor(rand() * 256)
          return { ch: chans[Math.floor(rand() * chans.length)], enc: rand() < 0.8 ? ENC_RAW : Math.floor(rand() * 4), rflags: Math.floor(rand() * 256), seq: Math.floor(rand() * 1e9), dtUs: Math.floor(rand() * 2e6) - 1e6, payload }
        })
        buf = batchFrame({ epoch: 1, frameSeq: k + 1, tSimNs: Math.floor(rand() * 1e12), flags: Math.floor(rand() * 16), records })
        const u8 = new Uint8Array(buf)
        if (rand() < 0.3) {
          // corrupt a length field or truncate
          const at = 16 + 4 + Math.floor(rand() * Math.max(1, u8.length - 24))
          if (at < u8.length) u8[at] = Math.floor(rand() * 256)
        }
        if (rand() < 0.2) buf = buf.slice(0, Math.floor(rand() * buf.byteLength))
        batches++
      }
      expect(() => r.sock.serverSend(buf)).not.toThrow()
      if (k % 7 === 0) expect(() => r.pull()).not.toThrow()
    }
    r.pull()
    const bad = r.host.counters.malformed
    expect(batches).toBeGreaterThan(5000)
    expect(bad).toBeGreaterThan(0)
    // a clean frame after the storm decodes normally
    r.pull()
    r.sock.serverSend(timeFrame({ epoch: 1, tSimNs: 1e9, tSrvNs: 1e9 }))
    r.sock.serverSend(batchFrame({ epoch: 1, frameSeq: 20_001, tSimNs: 9e9, records: [{ ch: CH.swarm, seq: 1, payload: lite32(3) }] }))
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.swarmN).toBe(3)
    expect(f.hdr.malformedFrames).toBe(bad)
    expect(r.host.connState).toBe('LIVE')
  })
})
