// ANET_Q16 v1 node payload (AWR-16 §4.3, §4.5) and the PointPool texel packing (M05 §6.2.4). Owner: M05.
// A node payload is SoA: pos u16[4n] (qx, qy, qz, oct16) then col u8[4n] (sRGB, class index) and optional ext u8[4n].
// A pool texel (RGBA32UI) holds one point: w0 = qx | qy<<16, w1 = qz | oct16<<16, w2 = r | g<<8 | b<<16 | class<<24,
// w3 = ext or 0. On little-endian platforms packing is an interleaving copy of 32-bit words.

/** Pack n points of the node payload at byteOffset into dst (4 words per point) at word offset dstOffset. */
export function packQ16(src: ArrayBuffer, byteOffset: number, n: number, bpp: 12 | 16, dst: Uint32Array, dstOffset: number): void {
  if (byteOffset % 4 !== 0) {
    // compression none guarantees byteOffset % 4 == 0 (AWR-16 §4.1 rule 6); copy when a caller passes a sliced view
    const copy = new Uint8Array(n * bpp)
    copy.set(new Uint8Array(src, byteOffset, n * bpp))
    packQ16(copy.buffer, 0, n, bpp, dst, dstOffset)
    return
  }
  const pos = new Uint32Array(src, byteOffset, 2 * n)
  const col = new Uint32Array(src, byteOffset + 8 * n, n)
  const ext = bpp === 16 ? new Uint32Array(src, byteOffset + 12 * n, n) : null
  let d = dstOffset
  for (let k = 0; k < n; k++) {
    dst[d] = pos[2 * k]
    dst[d + 1] = pos[2 * k + 1]
    dst[d + 2] = col[k]
    dst[d + 3] = ext ? ext[k] : 0
    d += 4
  }
}

/** decode point `local` of a packed node into world ENU (float64): p = cubeMin + q / 65535 * cubeSize */
export function decodePoint(packed: Uint32Array, local: number, cubeMin: Float64Array, o: number, cubeSize: number, out: Float64Array): void {
  const w0 = packed[4 * local]
  const w1 = packed[4 * local + 1]
  out[0] = cubeMin[o] + ((w0 & 0xffff) / 65535) * cubeSize
  out[1] = cubeMin[o + 1] + ((w0 >>> 16) / 65535) * cubeSize
  out[2] = cubeMin[o + 2] + ((w1 & 0xffff) / 65535) * cubeSize
}

/** oct16 normal decode (AWR-16 §4.5); returns false for 0x0000 (no normal) */
export function decodeOct16(w: number, out: Float64Array): boolean {
  if (w === 0) return false
  let x = ((w >>> 8) / 255) * 2 - 1
  let y = ((w & 255) / 255) * 2 - 1
  const z = 1 - Math.abs(x) - Math.abs(y)
  if (z < 0) {
    const ox = x
    x = (1 - Math.abs(y)) * (ox >= 0 ? 1 : -1)
    y = (1 - Math.abs(ox)) * (y >= 0 ? 1 : -1)
  }
  const l = Math.hypot(x, y, z)
  out[0] = x / l
  out[1] = y / l
  out[2] = z / l
  return true
}
