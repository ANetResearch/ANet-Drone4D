// Selected-vehicle readings from the server's EnvSample32 (uav/{id}/env raw items of the TelemetryFrame; M07-FR-058,
// FR-061; FX-WEB1): EnvSampleCache copies the newest valid record per vehicle out of the frame slot (the slot is reused
// after the next swap), skips other raw schemas (SensorPose48), expires after ES32_MAX_AGE_MS (the panel falls back to
// the local evaluation) and sampleReadings decodes wind, gust, MOR and effective rain with the layouts.json scales.
import { describe, expect, it } from 'vitest'
import { ES32, ES32_MAX_AGE_MS, EnvSampleCache, sampleReadings } from '@/engine/environment/summary'
import type { TelemetryFrame } from '@/net/rt/types'

interface Item { agentNo: number; schema: number; tSampleMs: number; fill?(dv: DataView, o: number): void }

/** a TelemetryFrame whose raw section holds the given items (80 B each, payload at +16) */
function frame(items: Item[]): TelemetryFrame {
  const base = 24
  const buf = new ArrayBuffer(base + ES32.item * items.length)
  const dv = new DataView(buf)
  items.forEach((it, k) => {
    const o = base + ES32.item * k
    dv.setUint16(o, it.agentNo, true)
    dv.setUint16(o + 2, 7, true)
    dv.setUint8(o + 4, it.schema)
    dv.setUint16(o + 6, it.schema === ES32.rawSchema ? ES32.size : 48, true)
    dv.setFloat64(o + 8, it.tSampleMs, true)
    it.fill?.(dv, o + ES32.payload)
  })
  return { raw: { count: items.length, bytes: dv, base } } as unknown as TelemetryFrame
}

/** EnvSample32 payload: wind (3, 4, 0) m/s going-to, sigma_ext, rain_eff, flags, gust */
const es32 = (valid: boolean, sigma = 0.002, gust = 1.25) => (dv: DataView, p: number): void => {
  dv.setFloat32(p + ES32.wind, 3, true)
  dv.setFloat32(p + ES32.wind + 4, 4, true)
  dv.setFloat32(p + ES32.wind + 8, 0, true)
  dv.setFloat32(p + ES32.sigmaExt, sigma, true)
  dv.setUint16(p + ES32.rainEff, 250, true) // 2.5 mm/h
  dv.setUint8(p + ES32.flags, valid ? 1 : 0)
  dv.setInt16(p + ES32.gust, Math.round(gust * 100), true)
}

describe('EnvSampleCache', () => {
  it('keeps the newest valid EnvSample32 per vehicle, skips other raw schemas', () => {
    const c = new EnvSampleCache()
    const f = frame([
      { agentNo: 3, schema: ES32.rawSchema, tSampleMs: 1000, fill: es32(true) },
      { agentNo: 4, schema: 2, tSampleMs: 1000 }, // SensorPose48
      { agentNo: 5, schema: ES32.rawSchema, tSampleMs: 1000, fill: es32(false) },
    ])
    expect(c.ingest(f, 100)).toBe(2)
    expect(c.get(3, 200)).not.toBeNull()
    expect(c.get(4, 200)).toBeNull() // no EnvSample32 for this vehicle
    expect(c.get(5, 200)).toBeNull() // flags.valid = 0
    // the frame slot is reused: the cached record must not alias it
    new Uint8Array(f.raw.bytes.buffer).fill(0)
    expect(c.get(3, 200)!.getFloat32(ES32.wind, true)).toBe(3)
  })
  it('expires after ES32_MAX_AGE_MS of wall time and refreshes on the next frame', () => {
    const c = new EnvSampleCache()
    c.ingest(frame([{ agentNo: 1, schema: ES32.rawSchema, tSampleMs: 0, fill: es32(true) }]), 0)
    expect(c.get(1, ES32_MAX_AGE_MS)).not.toBeNull()
    expect(c.get(1, ES32_MAX_AGE_MS + 1)).toBeNull()
    c.ingest(frame([{ agentNo: 1, schema: ES32.rawSchema, tSampleMs: 50, fill: es32(true, 0.004) }]), ES32_MAX_AGE_MS + 10)
    expect(c.get(1, ES32_MAX_AGE_MS + 20)!.getFloat32(ES32.sigmaExt, true)).toBeCloseTo(0.004, 7)
    c.clear()
    expect(c.get(1, ES32_MAX_AGE_MS + 20)).toBeNull()
  })
})

describe('sampleReadings', () => {
  it('decodes wind (going-to -> from), gust, MOR = k / sigma, rain and airspeed', () => {
    const c = new EnvSampleCache()
    c.ingest(frame([{ agentNo: 2, schema: ES32.rawSchema, tSampleMs: 0, fill: es32(true, 0.002, 1.25) }]), 0)
    const r = sampleReadings(c.get(2, 0)!, 3.912, [3, 4, 0], false)
    expect(r.source).toBe('sample32')
    expect(r.windMps).toBeCloseTo(5, 6)
    expect(r.speedMps).toBeCloseTo(5, 6)
    // going to (3, 4): blowing towards the north-east, i.e. from the south-west (about 216.87 deg)
    expect(r.dirFromDeg).toBeCloseTo(180 + (Math.atan2(3, 4) * 180) / Math.PI, 3)
    expect(r.gustMps).toBeCloseTo(1.25, 6)
    expect(r.morM).toBeCloseTo(3.912 / 0.002, 3)
    expect(r.rainEffMmh).toBeCloseTo(2.5, 6)
    expect(r.airspeedMps).toBeCloseTo(0, 6) // moving with the wind
    expect(r.stale).toBe(false)
    expect(Number.isNaN(sampleReadings(c.get(2, 0)!, 3.912, null, true).airspeedMps)).toBe(true)
  })
})
