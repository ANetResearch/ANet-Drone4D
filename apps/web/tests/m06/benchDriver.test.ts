// M06-AC-042 in Node (AWR-18 §8.6): the flight60 driver interpolates rows k and k + 1 of the 60 Hz [3601 x 6] buffer
// (<= 1e-3 m against a reference), refuses mismatching coordinate_sha256 or bin_sha256 (M06-E012 / PERF-E009), marks
// done at t >= 60 s; the M05 pure sampler agrees with M06's.
import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { Flight60Driver, FLIGHT60, flight60Sample, verifyFlight60, type Flight60Json } from '@/engine'
import { sampleFlight60 as m05Sample } from '@/engine/pointcloud/bench/flight60'

function makeBin(): Float32Array<ArrayBuffer> {
  const b = new Float32Array(FLIGHT60.frames * FLIGHT60.cols)
  for (let k = 0; k < FLIGHT60.frames; k++) {
    const t = k / 60
    b.set([100 * Math.cos(t / 10), 100 * Math.sin(t / 10), 50 + t, 0, 0, t], k * 6)
  }
  return b
}
const sha = (b: ArrayBuffer | Uint8Array): string => createHash('sha256').update(new Uint8Array(b as ArrayBuffer)).digest('hex')

describe('flight60 driver (M06-AC-042)', () => {
  it('linear interpolation between 60 Hz rows (<= 1e-3 m) and done at 60 s', () => {
    const buf = makeBin()
    const eye = new Float64Array(3)
    const tgt = new Float64Array(3)
    for (const t of [0, 1.2345, 30.5, 59.99]) {
      expect(flight60Sample(buf, t, eye, tgt)).toBe(true)
      const k = Math.floor(t * 60)
      const u = t * 60 - k
      for (let i = 0; i < 3; i++) expect(Math.abs(eye[i] - (buf[k * 6 + i] * (1 - u) + buf[(k + 1) * 6 + i] * u))).toBeLessThan(1e-3)
      const e2 = new Float64Array(3)
      const t2 = new Float64Array(3)
      m05Sample(buf, t, e2, t2)
      for (let i = 0; i < 3; i++) expect(Math.abs(e2[i] - eye[i])).toBeLessThan(1e-3)
    }
    expect(flight60Sample(buf, 60, eye, tgt)).toBe(false)
    const d = new Flight60Driver({ json: {} as Flight60Json, buf })
    d.step(1000)
    d.step(1000 + 60_000)
    expect(d.done).toBe(true)
  })

  it('refuses mismatching checksums (M06-E012 / PERF-E009)', async () => {
    const buf = makeBin()
    const coord = new TextEncoder().encode('{"worldId":"shenzhen"}')
    const json: Flight60Json = {
      schema: 'awr.flight60.v1', world_id: 'shenzhen', coordinate_sha256: sha(coord), bin_sha256: sha(buf.buffer), fps: 60, frames: 3601,
      fov_y_deg: 60, near_m: 1, far_m: 20000,
    }
    await expect(verifyFlight60(json, buf.buffer, coord.buffer as ArrayBuffer)).resolves.toMatchObject({ json })
    await expect(verifyFlight60({ ...json, bin_sha256: '0'.repeat(64) }, buf.buffer, coord.buffer as ArrayBuffer)).rejects.toThrow(/M06-E012/)
    await expect(verifyFlight60({ ...json, coordinate_sha256: '0'.repeat(64) }, buf.buffer, coord.buffer as ArrayBuffer)).rejects.toThrow(/PERF-E009/)
  })
})
