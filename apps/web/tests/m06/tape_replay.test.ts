// Front-end tape decoder against the internal replay (D3-AC-19 "front-end decoder scripted actor positions within 5 cm of the
// internal replay"; M23-AC-026; M27-FR-053). The golden packages/contracts/golden/event/tape-replay.{bin,json} holds the
// awr.TargetLite32.v1 rows that the internal replay actor source (awr.bench.recorder.read.TapeActorSource, extension point 11)
// produced from the tape attached to raw.mcap, at every second of the tape, random instants between ticks and around every
// state change (generated and re-verified by tests/d3/bench/test_replay_actors.py).
import { describe, expect, it } from 'vitest'
import { TapeReader } from '../../src/engine/actors/tape'
import { decodeTargetLite32, newTargetRows } from '../../src/engine/targets/decode'
import { readBytes, readJson, sha256Hex } from '../contracts/util'

interface RosterEntry { agent_no: number; id: string; label: string; kind: string; class: string; class_code: number }
interface Sample { t_sim_ns: number; rows: string }
interface Golden {
  schema: string
  content_sha256: string
  file_bytes: number
  offset_ns: number
  tol_m: number
  row_bytes: number
  n_scripted: number
  roster: RosterEntry[]
  samples: Sample[]
}

const g = readJson<Golden>('golden/event/tape-replay.json')
const bin = readBytes('golden/event/tape-replay.bin')
const VISIBLE = (s: number): boolean => s === 1 || s === 2

const rowsAt = (s: Sample): DataView => {
  const b = Buffer.from(s.rows, 'base64')
  return new DataView(b.buffer, b.byteOffset, b.byteLength)
}

describe('tape decoder vs internal replay', () => {
  const r = new TapeReader(bin)
  const ids = g.roster.map((e) => e.agent_no)

  it('decodes the same tape the internal replay used', () => {
    expect(g.schema).toBe('awr.golden.tape_replay.v1')
    expect(bin.byteLength).toBe(g.file_bytes)
    const body = r.canonicalBody()
    const all = new Uint8Array(r.headerBytes.byteLength + body.byteLength)
    all.set(r.headerBytes)
    all.set(body, r.headerBytes.byteLength)
    expect(sha256Hex(all)).toBe(g.content_sha256)
    expect(r.nScripted).toBe(g.n_scripted)
    expect(g.roster.map((e) => e.label)).toEqual(r.header.actors.slice(0, r.nScripted).map((a) => a.id))
    expect(g.tol_m).toBe(0.05)
  })

  it('scripted actor positions stay within 5 cm of the replay rows at every sample', () => {
    const out = newTargetRows()
    let worst = 0
    const states = new Set<number>()
    for (const s of g.samples) {
      const dv = rowsAt(s)
      expect(dv.byteLength).toBe(g.row_bytes * ids.length)
      decodeTargetLite32(dv, ids.length, out)
      const st = r.actorStates(g.offset_ns + s.t_sim_ns, ids)
      for (let i = 0; i < ids.length; i++) {
        expect(out.agentNo[i]).toBe(ids[i])
        expect(out.cls[i]).toBe(g.roster[i].class_code)
        const d = Math.hypot(out.pos[3 * i] - st.pos[3 * i], out.pos[3 * i + 1] - st.pos[3 * i + 1], out.pos[3 * i + 2] - st.pos[3 * i + 2])
        worst = Math.max(worst, d)
        // the replay shows an actor exactly when the decoder considers it visible (moving or waiting)
        expect(out.enabled[i]).toBe(VISIBLE(st.state[i]) ? 1 : 0)
        const dy = Math.abs(((out.heading[i] - st.yaw[i] + 3 * Math.PI) % (2 * Math.PI)) - Math.PI)
        expect(dy).toBeLessThanOrEqual(1e-6)
        states.add(st.state[i])
      }
    }
    expect(worst).toBeLessThanOrEqual(g.tol_m)
    expect(worst).toBeLessThanOrEqual(0.004) // f32 quantization of the replay rows only
    for (const s of [1, 2, 3, 4]) expect(states.has(s)).toBe(true) // moving, waiting, in vehicle, inside a building
  })

  it('a playhead held on the previous tick would exceed the threshold (the comparison is sensitive)', () => {
    const out = newTargetRows()
    let worst = 0
    for (const s of g.samples) {
      if (s.t_sim_ns <= 0) continue
      decodeTargetLite32(rowsAt(s), ids.length, out)
      const st = r.actorStates(Math.floor(s.t_sim_ns / 1e9) * 1e9 - 1e8, ids)
      for (let i = 0; i < ids.length; i++) {
        if (!out.enabled[i]) continue
        worst = Math.max(worst, Math.hypot(out.pos[3 * i] - st.pos[3 * i], out.pos[3 * i + 1] - st.pos[3 * i + 1]))
      }
    }
    expect(worst).toBeGreaterThan(g.tol_m)
  })
})
