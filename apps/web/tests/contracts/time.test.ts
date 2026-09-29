// TIME frame golden (AWR-17 §6.10; M12-FR-006): state low 4 bits, bit7 REPLAY, reserved bits ignored
import { describe, expect, it } from 'vitest'
import { advances, isReplay, newTimeView, readTime, timeState, writeTime, TIME_SIZE } from '@awr/contracts/time'
import { TimeState } from '@awr/contracts/enums'
import { bytesToHex, hexToBytes, readJson } from './util'

interface Case { hex: string; state: number; replay: boolean; reserved_bits: number; epoch: number; rate: number; t_sim_ns: number; t_srv_ns: number; advances: boolean }
const cases = readJson<{ cases: Case[] }>('golden/rt/time.json').cases

describe('TIME golden', () => {
  it('covers 10 states x replay x reserved bits', () => {
    expect(new Set(cases.map((c) => c.state)).size).toBe(10)
    expect(cases.length).toBe(40)
  })

  it.each(cases.map((c, i) => [i, c] as const))('case %i decodes like Python', (_i, c) => {
    const b = hexToBytes(c.hex)
    expect(b.byteLength).toBe(TIME_SIZE)
    const t = readTime(new DataView(b.buffer), 0, newTimeView())
    expect([t.state, t.replay, t.epoch, t.rate, t.t_sim_ns, t.t_srv_ns]).toEqual([c.state, c.replay, c.epoch, c.rate, c.t_sim_ns, c.t_srv_ns])
    expect([timeState(b[1]), isReplay(b[1]), advances(b[1])]).toEqual([c.state, c.replay, c.advances])
    expect(c.advances).toBe(c.state === TimeState.PLAYING || c.state === TimeState.LIVE)
    if (c.reserved_bits === 0) {
      const out = new Uint8Array(TIME_SIZE)
      writeTime(new DataView(out.buffer), 0, t)
      expect(bytesToHex(out)).toBe(c.hex)
    }
  })

  it('rejects non-TIME opcodes', () => {
    const b = hexToBytes(cases[0].hex)
    b[0] = 0x10
    expect(() => readTime(new DataView(b.buffer), 0, newTimeView())).toThrow()
  })
})
