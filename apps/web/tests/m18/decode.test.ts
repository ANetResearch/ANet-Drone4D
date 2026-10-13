// M18-AC-016 (TS part): awr.TargetLite32.v1 golden vectors (AWR-17 §17.7.3, packages/contracts/golden/layouts/target_vectors.json)
// decode to the same fields as the Python layout; bit fields (awareness, criterion, moving, occupied, sound, enabled, level,
// captured, relay, sensor, hidden, discovered_recent, cfg_rev mod 64); the model ingests rows and drops stale details.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { decodeTargetLite32, newTargetRows, rowsBytes, TargetsModel } from '@/engine/targets'

const ROOT = join(import.meta.dirname, '../../../..')
const G = JSON.parse(readFileSync(join(ROOT, 'packages/contracts/golden/layouts/target_vectors.json'), 'utf8')) as {
  vectors: { name: string; hex: string; decoded: Record<string, number | number[]> }[]
}
const hexBytes = (h: string): Uint8Array => Uint8Array.from(h.match(/../g)!.map((x) => parseInt(x, 16)))

describe('TargetLite32 decode (M18-AC-016)', () => {
  it('golden vectors', () => {
    const bytes = new Uint8Array(G.vectors.length * 32)
    G.vectors.forEach((v, i) => bytes.set(hexBytes(v.hex), i * 32))
    const out = newTargetRows()
    decodeTargetLite32(new DataView(bytes.buffer), G.vectors.length, out)
    expect(out.n).toBe(3)
    G.vectors.forEach((v, i) => {
      const d = v.decoded
      expect(out.agentNo[i]).toBe(d.agent_no)
      expect(out.cls[i]).toBe(d.class)
      const st = d.state as number
      expect(out.aware[i]).toBe(st & 7)
      expect(out.physical[i]).toBe((st >> 3) & 1)
      expect(out.moving[i]).toBe((st >> 4) & 1)
      expect(out.occupied[i]).toBe((st >> 5) & 1)
      expect(out.soundOn[i]).toBe((st >> 6) & 1)
      expect(out.enabled[i]).toBe((st >> 7) & 1)
      const p = d.pos as number[]
      expect([out.pos[3 * i], out.pos[3 * i + 1], out.pos[3 * i + 2]]).toEqual(p)
      expect(out.heading[i]).toBeCloseTo(d.heading_rad as number, 6)
      expect(out.speed[i]).toBeCloseTo((d.speed_cms as number) * 0.01, 6)
      const b = d.best as number
      expect([out.level[i], out.captured[i], out.relay[i], out.sensor[i]]).toEqual([b & 3, (b >> 2) & 1, (b >> 3) & 1, b >> 4])
      expect(out.alertness[i]).toBeCloseTo((d.alertness_u8 as number) / 255, 6)
      expect(out.discovered[i]).toBe(d.discovered_n)
      expect(out.nVol[i]).toBe(d.n_vol)
      const f = d.flags as number
      expect([out.hidden[i], out.discoveredRecent[i], out.cfgRev[i]]).toEqual([f & 1, (f >> 1) & 1, f >> 2])
      expect(out.keepoutR[i]).toBe(d.keepout_r_m)
    })
    // the third vector: dog REACTING, physical, occupied, barking, LWIR detection (17 §17.7.3)
    expect([out.cls[2], out.aware[2], out.physical[2], out.occupied[2], out.soundOn[2], out.sensor[2], out.discoveredRecent[2]]).toEqual([4, 2, 1, 1, 1, 3, 1])
  })

  it('rows payload forms and the model', () => {
    const bytes = hexBytes(G.vectors[0].hex)
    expect(rowsBytes(bytes)).toBe(bytes)
    expect(rowsBytes({ rows: bytes.buffer })?.byteLength).toBe(32)
    expect(rowsBytes('x')).toBeNull()
    const m = new TargetsModel()
    m.resolver = { targetIdOf: (n) => (n === 0 ? 'person-01' : undefined) }
    m.ingestDetail('person-01', { id: 'person-01', class: 'person', awareness: 'UNAWARE', alertness: 0.5, criterion_mode: 'geometric',
      discovered_count: 0, sensitivity: [], cfg_rev: 1 })
    m.ingestDetail('ghost-01', { id: 'ghost-01', class: 'dog', awareness: 'UNAWARE', alertness: 0.5, criterion_mode: 'geometric',
      discovered_count: 0, sensitivity: [], cfg_rev: 1 })
    expect(m.ingest({ topic: 'swarm/target/state', data: bytes, tSimMs: 1500 })).toBe(true)
    expect(m.rows.n).toBe(1)
    expect(m.idOfRow(0)).toBe('person-01')
    expect(m.details.has('ghost-01')).toBe(false)
    expect(m.ingest({ topic: 'uav/u1/state_ext', data: {} })).toBe(false)
  })
})
