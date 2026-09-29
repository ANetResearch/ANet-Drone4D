// AWRV v1 volume decoding (M07-FR-051; 16 §8.3; layouts.json awr.env.AwrvHeader.v1; g06 §7.3). Owner: M07.
// Little endian, 64 B header + payload; layout 0 = zyx C order (x fastest = Data3DTexture(width nx, height ny, depth nz));
// dtype 1 f16, 2 f32, 3 u8; zlib CRC-32 over the payload. magic, version, payload_bytes or CRC mismatch are rejected
// (V-E-04, 443 ENV_ASSET_INVALID). The f16 payload stays as raw half floats for the HalfFloat texture; `cpu` is the exact
// float32 mirror used by the CPU sampler (same asset on both ends, M07-AC-016).
import { Data3DTexture, DataTexture, HalfFloatType, LinearFilter, RepeatWrapping, RGBAFormat, UnsignedByteType } from 'three'

export const AWRV_MAGIC = 0x56525741
export const ENV_ASSET_INVALID = 443

export interface AwrvVolume {
  kind: number
  nx: number
  ny: number
  nz: number
  comp: number
  dtype: 1 | 2 | 3
  origin: [number, number, number]
  cell: [number, number, number]
  dirFromDeg: number
  valueScale: number
  fieldVersion: number
  /** raw payload view: Uint16Array (f16 bits), Float32Array or Uint8Array */
  raw: Uint16Array | Float32Array | Uint8Array
  /** float32 values (f16 and f32 payloads), null for u8 */
  cpu: Float32Array | null
}

export class AwrvError extends Error {
  readonly code = ENV_ASSET_INVALID
}

let CRC_TABLE: Uint32Array | null = null
export function crc32(b: Uint8Array): number {
  if (!CRC_TABLE) {
    CRC_TABLE = new Uint32Array(256)
    for (let n = 0; n < 256; n++) {
      let c = n
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
      CRC_TABLE[n] = c >>> 0
    }
  }
  let c = 0xffffffff
  for (let i = 0; i < b.length; i++) c = CRC_TABLE[(c ^ b[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

/** IEEE 754 binary16 -> number (exact) */
export function halfToFloat(h: number): number {
  const s = h & 0x8000 ? -1 : 1
  const e = (h >> 10) & 0x1f
  const f = h & 0x3ff
  if (e === 0) return s * f * 2 ** -24
  if (e === 31) return f ? Number.NaN : s * Number.POSITIVE_INFINITY
  return s * (1 + f / 1024) * 2 ** (e - 15)
}

const SIZE = { 1: 2, 2: 4, 3: 1 } as const

export function decodeAWRV(buf: ArrayBuffer, checkCrc = true): AwrvVolume {
  if (buf.byteLength < 64) throw new AwrvError('AWRV too short')
  const dv = new DataView(buf)
  if (dv.getUint32(0, true) !== AWRV_MAGIC) throw new AwrvError('AWRV magic mismatch')
  if (dv.getUint16(4, true) !== 1) throw new AwrvError('AWRV version')
  const kind = dv.getUint16(6, true)
  const nx = dv.getUint16(8, true)
  const ny = dv.getUint16(10, true)
  const nz = dv.getUint16(12, true)
  const comp = dv.getUint16(14, true)
  const dtype = dv.getUint8(16) as 1 | 2 | 3
  if (!(dtype in SIZE)) throw new AwrvError(`AWRV dtype ${dtype}`)
  const payloadBytes = dv.getUint32(56, true)
  if (payloadBytes !== nx * ny * nz * comp * SIZE[dtype]) throw new AwrvError('AWRV payload_bytes mismatch')
  if (buf.byteLength < 64 + payloadBytes) throw new AwrvError('AWRV payload truncated')
  const payload = new Uint8Array(buf, 64, payloadBytes)
  if (checkCrc && crc32(payload) !== dv.getUint32(60, true)) throw new AwrvError('AWRV CRC mismatch')
  // copy so typed views are aligned regardless of the source buffer offset
  const body = payload.slice().buffer
  let raw: Uint16Array | Float32Array | Uint8Array
  let cpu: Float32Array | null = null
  if (dtype === 1) {
    raw = new Uint16Array(body)
    cpu = new Float32Array(raw.length)
    for (let i = 0; i < raw.length; i++) cpu[i] = halfToFloat(raw[i])
  } else if (dtype === 2) {
    raw = new Float32Array(body)
    cpu = raw
  } else raw = new Uint8Array(body)
  return {
    kind, nx, ny, nz, comp, dtype,
    origin: [dv.getFloat32(20, true), dv.getFloat32(24, true), dv.getFloat32(28, true)],
    cell: [dv.getFloat32(32, true), dv.getFloat32(36, true), dv.getFloat32(40, true)],
    dirFromDeg: dv.getFloat32(44, true), valueScale: dv.getFloat32(48, true), fieldVersion: dv.getUint32(52, true), raw, cpu,
  }
}

/** HalfFloat RGBA Data3DTexture (repeat, linear) of an f16 volume with comp = 4 (turbulence box) */
export function volumeTexture3D(v: AwrvVolume): Data3DTexture {
  if (v.dtype !== 1 || v.comp !== 4) throw new AwrvError('3D texture needs an f16 RGBA volume')
  const t = new Data3DTexture(v.raw as Uint16Array, v.nx, v.ny, v.nz)
  t.format = RGBAFormat
  t.type = HalfFloatType
  t.minFilter = LinearFilter
  t.magFilter = LinearFilter
  t.wrapS = RepeatWrapping
  t.wrapT = RepeatWrapping
  t.wrapR = RepeatWrapping
  t.generateMipmaps = false
  t.unpackAlignment = 1
  t.needsUpdate = true
  return t
}

/** RGBA8 DataTexture (repeat, linear) of a 2D u8 volume (weather map, nz = 1) */
export function volumeTexture2D(v: AwrvVolume): DataTexture {
  if (v.dtype !== 3 || v.comp !== 4 || v.nz !== 1) throw new AwrvError('2D texture needs a u8 RGBA volume with nz = 1')
  const t = new DataTexture(v.raw as Uint8Array, v.nx, v.ny, RGBAFormat, UnsignedByteType)
  t.minFilter = LinearFilter
  t.magFilter = LinearFilter
  t.wrapS = RepeatWrapping
  t.wrapT = RepeatWrapping
  t.generateMipmaps = false
  t.needsUpdate = true
  return t
}
