// ANET_Q16 v1 decoding and PointPool packing (AWR-16 §4.3, §4.5; M05 §6.2.4; M05-AC-005 golden): the Shenzhen root
// sample point of AWR-16 §4.3 packs to w0 0x8c9d6195, w1 0x80803210, w2 0x05f5f3f2, w3 0 and decodes to
// (-162.0115, 98.5110, 374.0077) with an oct16 normal of about +Z.
import { describe, expect, it } from 'vitest'
import { decodeOct16, decodePoint, packQ16 } from '@/engine/pointcloud/io/q16'

const hex = (s: string): Uint8Array => Uint8Array.from(s.split(' ').map((x) => parseInt(x, 16)))

describe('ANET_Q16 golden (AWR-16 §4.3)', () => {
  // one-point node: pos u16[4] then col u8[4]
  const node = new Uint8Array(12)
  node.set(hex('95 61 9d 8c 10 32 80 80'), 0)
  node.set(hex('f2 f3 f5 05'), 8)

  it('packs into pool texel words', () => {
    const dst = new Uint32Array(4)
    packQ16(node.buffer, 0, 1, 12, dst, 0)
    expect(Array.from(dst)).toEqual([0x8c9d6195, 0x80803210, 0x05f5f3f2, 0])
  })

  it('decodes position within half a quantisation step and the class index', () => {
    const dst = new Uint32Array(4)
    packQ16(node.buffer, 0, 1, 12, dst, 0)
    const cubeMin = Float64Array.from([-924.0189514160156, -999.5227966308594, -16.924943923950195])
    const size = 1999.0455962607646
    const p = new Float64Array(3)
    decodePoint(dst, 0, cubeMin, 0, size, p)
    expect(p[0]).toBeCloseTo(-162.0115, 3)
    expect(p[1]).toBeCloseTo(98.511, 3)
    expect(p[2]).toBeCloseTo(374.0077, 3)
    const half = size / 65535 / 2
    expect(Math.abs(p[0] - -162.0)).toBeLessThanOrEqual(half)
    expect(Math.abs(p[1] - 98.5)).toBeLessThanOrEqual(half)
    expect(Math.abs(p[2] - 374.0)).toBeLessThanOrEqual(half)
    expect(dst[2] >>> 24).toBe(5) // building_roof class index
  })

  it('oct16 normal: 0x8080 is +Z, 0 is "no normal", the lower hemisphere folds', () => {
    const n = new Float64Array(3)
    expect(decodeOct16(0x8080, n)).toBe(true)
    expect(n[0]).toBeCloseTo(0.004, 3)
    expect(n[1]).toBeCloseTo(0.004, 3)
    expect(n[2]).toBeCloseTo(0.99998, 4)
    expect(decodeOct16(0, n)).toBe(false)
    // (255, 255) and 0xFFFF decode to -Z (AWR-16 §4.5)
    decodeOct16(0xffff, n)
    expect(n[2]).toBeCloseTo(-1, 6)
    // round trip of an encoded direction (the Python encoder of AWR-16 §4.5 transcribed)
    const enc = (x: number, y: number, z: number): number => {
      const l = Math.abs(x) + Math.abs(y) + Math.abs(z)
      let ox = x / l
      let oy = y / l
      if (z < 0) {
        const sx = x >= 0 ? 1 : -1
        const sy = y >= 0 ? 1 : -1
        const ax = Math.abs(ox)
        ox = (1 - Math.abs(oy)) * sx
        oy = (1 - ax) * sy
      }
      const u = Math.min(255, Math.max(0, Math.round((ox * 0.5 + 0.5) * 255)))
      const v = Math.min(255, Math.max(0, Math.round((oy * 0.5 + 0.5) * 255)))
      const w = (u << 8) | v
      return w === 0 ? 0xffff : w
    }
    for (const d of [[0.3, -0.5, 0.8], [-0.7, 0.2, -0.6], [0.1, 0.99, 0.05], [0.5, 0.5, -0.7]]) {
      const l = Math.hypot(d[0], d[1], d[2])
      decodeOct16(enc(d[0] / l, d[1] / l, d[2] / l), n)
      const dot = (n[0] * d[0] + n[1] * d[1] + n[2] * d[2]) / l
      expect(Math.acos(Math.min(1, dot)) * (180 / Math.PI)).toBeLessThan(1) // max error 0.95 degrees (AWR-16 §4.5)
    }
  })

  it('packs a 16-byte-per-point node with the ext stream and handles unaligned offsets', () => {
    const n = 3
    const buf = new ArrayBuffer(4 + 16 * n)
    const dv = new DataView(buf, 4)
    for (let k = 0; k < n; k++) {
      dv.setUint32(8 * k, 0x11110000 + k, true)
      dv.setUint32(8 * k + 4, 0x22220000 + k, true)
      dv.setUint32(8 * n + 4 * k, 0x33330000 + k, true)
      dv.setUint32(12 * n + 4 * k, 0x44440000 + k, true)
    }
    const dst = new Uint32Array(4 * n)
    packQ16(buf, 4, n, 16, dst, 0)
    expect(Array.from(dst.subarray(4, 8))).toEqual([0x11110001, 0x22220001, 0x33330001, 0x44440001])
    const odd = new Uint8Array(3 + 12)
    odd.set(new Uint8Array(buf, 4, 12), 3)
    const d2 = new Uint32Array(4)
    packQ16(odd.buffer, 3, 1, 12, d2, 0)
    expect(d2[0]).toBe(0x11110000)
  })
})
