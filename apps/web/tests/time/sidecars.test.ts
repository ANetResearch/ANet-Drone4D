// Sidecar parsers and the marker table against the Python writer (M12 §7.5.1, §7.5.2; FR-037; contract parity with
// python/awr/recorder/sidecar.py): sample.ovw / sample.evx were written by awr.recorder.synth (3 vehicles, 4.5 s, one
// critical event every 2 s) and sample.expected.json holds the Python decoding; the TS marker rules equal the draft
// contract tests/recorder/fixtures/markers.json shared with the backend; loading an .evx fills the track model.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { MARKER_RULES, MarkerClass, TrackModel, parseEvx, parseOvw, ovwBinMs } from '@/engine/time/index'

const fx = (n: string): ArrayBuffer => {
  const b = readFileSync(new URL(`./fixtures/${n}`, import.meta.url))
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer
}
interface Expected {
  evx: { n: number; closed: boolean; t_ms: number[]; level: number[]; marker: number[]; agent: number[]; mseq: number[] }
  ovw: { n_bins: number; n_tracks: number; tracks: number[]; bin_ns: number; bins: { n_present: number; ev: number[]; flags: number; track_z: number[] }[] }
}
const exp = JSON.parse(readFileSync(new URL('./fixtures/sample.expected.json', import.meta.url), 'utf8')) as Expected

describe('sidecar parity with the Python writer', () => {
  it('parses the .evx index like Python', () => {
    const e = parseEvx(fx('sample.evx'))
    expect(e.closed).toBe(exp.evx.closed)
    expect(e.n).toBe(exp.evx.n)
    expect(Array.from(e.t)).toEqual(exp.evx.t_ms)
    expect(Array.from(e.level)).toEqual(exp.evx.level)
    expect(Array.from(e.marker)).toEqual(exp.evx.marker)
    expect(Array.from(e.agentNo)).toEqual(exp.evx.agent)
    expect(Array.from(e.mseq)).toEqual(exp.evx.mseq)
    expect(Array.from(e.marker).filter((m) => m === MarkerClass.CRITICAL).length).toBe(2)
  })
  it('parses the .ovw index like Python', () => {
    const o = parseOvw(fx('sample.ovw'))
    expect(o.closed).toBe(true)
    expect(o.nBins).toBe(exp.ovw.n_bins)
    expect(o.nTracks).toBe(exp.ovw.n_tracks)
    expect(Array.from(o.trackAgentNo)).toEqual(exp.ovw.tracks)
    expect(o.binNs).toBe(exp.ovw.bin_ns)
    exp.ovw.bins.forEach((b, i) => {
      expect(o.nPresent[i]).toBe(b.n_present)
      expect(Array.from(o.evByLevel.subarray(4 * i, 4 * i + 4))).toEqual(b.ev)
      expect(o.binFlags[i]).toBe(b.flags)
      for (let k = 0; k < o.nTracks; k++) expect(o.trackPos[3 * (i * o.nTracks + k) + 2]).toBeCloseTo(b.track_z[k], 4)
    })
    expect(ovwBinMs(o, 2)).toBe(2000)
  })
  it('an open .evx (no count in the header) is read by size', () => {
    const b = new Uint8Array(fx('sample.evx'))
    b[6] = 0 // clear CLOSED
    new DataView(b.buffer).setUint32(12, 0, true)
    const e = parseEvx(b.buffer)
    expect(e.closed).toBe(false)
    expect(e.n).toBe(exp.evx.n)
  })
  it('loading the .evx fills the track model (NONE markers skipped)', () => {
    const e = parseEvx(fx('sample.evx'))
    const m = new TrackModel()
    m.loadEvx(e.n, e.t, e.level, e.marker, e.agentNo, e.mseq)
    expect(m.markers.n).toBe(Array.from(e.marker).filter((x) => x !== 0).length)
    m.evalHero(0)
    expect(m.heroIdx).toBeGreaterThanOrEqual(0)
  })
})

describe('marker table parity', () => {
  it('equals the draft contract shared with python/awr/recorder/sidecar.py', () => {
    const draft = JSON.parse(readFileSync(new URL('../../../../tests/recorder/fixtures/markers.json', import.meta.url), 'utf8')) as unknown
    expect(JSON.parse(JSON.stringify(MARKER_RULES))).toEqual(draft)
  })
})
