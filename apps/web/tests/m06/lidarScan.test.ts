// D2 WP-11 (P1) MID-360 scan points (M13 §7.2.4 awr.LidarScan8.v1, §6.5.12): header with and without the 16 B generic blob
// header; points rotated by the frame's WORLD<-SENSOR quaternion and shifted by its origin (<= 1 cm, the i16 cm grid);
// a frame larger than half the ring is subsampled evenly; the ring wraps with two upload ranges; points stay drawn while
// the newest frame is younger than the persistence (0.1-2 s, clamped) and the layer is one draw.
import { describe, expect, it } from 'vitest'
import { Quaternion, Vector3 } from 'three'
import { LIDAR_SCAN, LidarScanLayer, decodeLidarScanHeader, newLidarScanHeader } from '@/engine'
import { GLPointsNodeMaterial } from '@/viewport/glPointsNodeMaterial'

function frame(points: number[][], pos: number[], q: Quaternion, blob = false): Uint8Array {
  const o = blob ? 16 : 0
  const buf = new ArrayBuffer(o + 48 + 8 * points.length)
  const dv = new DataView(buf)
  if (blob) {
    dv.setUint32(0, 0x42525741, true)
    dv.setUint16(4, 1, true)
    dv.setUint16(6, 3, true)
    dv.setUint32(8, 48 + 8 * points.length, true)
    dv.setUint32(12, 1, true)
  }
  dv.setBigInt64(o, 123_000_000n, true)
  dv.setUint32(o + 8, 7, true)
  dv.setUint32(o + 12, points.length, true)
  for (let k = 0; k < 3; k++) dv.setFloat32(o + 16 + 4 * k, pos[k], true)
  ;[q.x, q.y, q.z, q.w].forEach((v, k) => dv.setFloat32(o + 28 + 4 * k, v, true))
  dv.setUint16(o + 44, 1, true)
  dv.setUint16(o + 46, 1, true)
  points.forEach((p, i) => {
    const b = o + 48 + 8 * i
    dv.setInt16(b, Math.round(p[0] * 100), true)
    dv.setInt16(b + 2, Math.round(p[1] * 100), true)
    dv.setInt16(b + 4, Math.round(p[2] * 100), true)
    dv.setUint8(b + 6, p[3] ?? 128)
    dv.setUint8(b + 7, 0)
  })
  return new Uint8Array(buf)
}

describe('MID-360 scan points (LidarScan8)', () => {
  it('header, transform to ENU, AWRB prefix', () => {
    const q = new Quaternion().setFromAxisAngle(new Vector3(0, 0, 1), 0.6).multiply(new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), -0.4))
    const pos = [120.5, -40.25, 33]
    const pts = [[10, 0, 0, 200], [3.21, -4.5, 1.25, 30], [-20, 15, -2, 255]]
    for (const blob of [false, true]) {
      const bytes = frame(pts, pos, q, blob)
      const h = newLidarScanHeader()
      expect(decodeLidarScanHeader(new DataView(bytes.buffer), h)).toBe(true)
      expect([h.seq, h.n, h.tFrameNs, h.flags]).toEqual([7, 3, 123_000_000, 1])
      const L = new LidarScanLayer(64, new GLPointsNodeMaterial())
      expect(L.ingest(bytes, 10)).toBe(3)
      const out = new Float64Array(3)
      pts.forEach((p, i) => {
        const w = new Vector3(p[0], p[1], p[2]).applyQuaternion(q).add(new Vector3(pos[0], pos[1], pos[2]))
        L.pointAt(i, out)
        expect(Math.hypot(out[0] - w.x, out[1] - w.y, out[2] - w.z)).toBeLessThan(0.01)
      })
      L.dispose()
    }
    expect(decodeLidarScanHeader(new DataView(new ArrayBuffer(20)), newLidarScanHeader())).toBe(false)
  })
  it('subsampling, ring wrap, persistence and draws', () => {
    const q = new Quaternion()
    const L = new LidarScanLayer(100, new GLPointsNodeMaterial())
    const big = Array.from({ length: 400 }, (_, i) => [i / 10, 0, 0, i % 256])
    expect(L.ingest(frame(big, [0, 0, 0], q), 1)).toBe(50) // <= half the ring
    expect(L.ingest(frame(big.slice(0, 30), [0, 0, 0], q), 1.4)).toBe(30)
    expect(L.ingest(frame(big.slice(0, 40), [0, 0, 0], q), 1.8)).toBe(40) // wraps: 80 + 40 > 100
    const pa = (L as unknown as { pos: { updateRanges: { start: number; count: number }[] } }).pos
    expect(pa.updateRanges.length).toBe(2)
    expect(pa.updateRanges[0]).toEqual({ start: 80 * 3, count: 20 * 3 })
    expect(pa.updateRanges[1]).toEqual({ start: 0, count: 20 * 3 })
    L.update(2.0)
    expect(L.points.visible).toBe(true)
    expect(L.points.geometry.drawRange.count).toBe(100)
    expect(L.drawCount()).toBe(1)
    L.update(1.8 + LIDAR_SCAN.persistS + 0.01)
    expect(L.drawCount()).toBe(0)
    L.setPersist(9)
    expect(L.persistS).toBe(LIDAR_SCAN.persistMaxS)
    L.setPersist(0)
    expect(L.persistS).toBe(LIDAR_SCAN.persistMinS)
    L.clear()
    L.update(1.9)
    expect(L.drawCount()).toBe(0)
    expect(L.frames).toBe(3)
    L.dispose()
  })
})
