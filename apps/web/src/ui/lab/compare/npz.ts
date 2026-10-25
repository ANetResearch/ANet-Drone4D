// NumPy `.npz` 与 `.npy` 解码（只读，浏览器与 Node 通用）：两档对照的轨迹制品 `<case>.<tier>.npz` 由 `awr.phys.tier_compare.CaseRun.save`
// 以 `np.savez` 写出（ZIP 存储方式，每个成员以 force_zip64 打开，因此本地文件头的大小字段为 0xFFFFFFFF 并带 ZIP64 扩展字段；
// 中央目录给出真实大小）。本模块按中央目录定位成员，以本地文件头的名称与扩展字段长度求数据起点，校验 CRC-32；存储方式（0）直接取字节，
// 压缩方式（8，`np.savez_compressed`）经 DecompressionStream('deflate-raw') 解压。`.npy` 支持格式版本 1.0、2.0、3.0，小端 f8、f4、
// i8、i4、i2、u1、u2、u4、u8 与 C 顺序；其余（大端、Fortran 顺序、对象数组）拒绝而不猜测。
export type NpyDtype = 'f8' | 'f4' | 'i8' | 'i4' | 'i2' | 'u1' | 'u2' | 'u4' | 'u8'
export type NpyData = Float64Array | Float32Array | BigInt64Array | Int32Array | Int16Array | Uint8Array | Uint16Array | Uint32Array | BigUint64Array
export interface NpyArray { dtype: NpyDtype; shape: readonly number[]; data: NpyData }

/** 解码失败（`code`：`zip`、`crc`、`method`、`npy`、`dtype`、`order`、`size`） */
export class NpzError extends Error {
  readonly code: string
  constructor(code: string, message?: string) {
    super(message ?? code)
    this.name = 'NpzError'
    this.code = code
  }
}

const EOCD = 0x06054b50
const EOCD64_LOC = 0x07064b50
const EOCD64 = 0x06064b50
const CEN = 0x02014b50
const LOC = 0x04034b50
const U32_MAX = 0xffffffff

let crcTable: Uint32Array | null = null
/** CRC-32（IEEE 802.3，ZIP 使用的多项式 0xEDB88320） */
export function crc32(b: Uint8Array): number {
  if (!crcTable) {
    crcTable = new Uint32Array(256)
    for (let n = 0; n < 256; n++) {
      let c = n
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
      crcTable[n] = c >>> 0
    }
  }
  const tb = crcTable
  let c = 0xffffffff
  for (let i = 0; i < b.length; i++) c = tb[(c ^ b[i]!) & 0xff]! ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

const u64 = (v: DataView, o: number): number => {
  const x = v.getBigUint64(o, true)
  if (x > BigInt(Number.MAX_SAFE_INTEGER)) throw new NpzError('zip', 'offset beyond 2^53')
  return Number(x)
}

export interface ZipEntry { name: string; method: number; crc: number; csize: number; usize: number; local: number }

/** 中央目录的成员清单（含 ZIP64 结束记录与扩展字段） */
export function zipEntries(buf: ArrayBuffer): ZipEntry[] {
  const v = new DataView(buf)
  const len = buf.byteLength
  let e = -1
  for (let i = len - 22; i >= Math.max(0, len - 22 - 0xffff); i--) {
    if (v.getUint32(i, true) === EOCD) {
      e = i
      break
    }
  }
  if (e < 0) throw new NpzError('zip', 'end of central directory not found')
  let count = v.getUint16(e + 10, true)
  let cdOff = v.getUint32(e + 16, true)
  if ((count === 0xffff || cdOff === U32_MAX) && e >= 20 && v.getUint32(e - 20, true) === EOCD64_LOC) {
    const z = u64(v, e - 20 + 8)
    if (z + 56 > len || v.getUint32(z, true) !== EOCD64) throw new NpzError('zip', 'bad zip64 end record')
    count = u64(v, z + 32)
    cdOff = u64(v, z + 48)
  }
  const dec = new TextDecoder()
  const out: ZipEntry[] = []
  let p = cdOff
  for (let k = 0; k < count; k++) {
    if (p + 46 > len || v.getUint32(p, true) !== CEN) throw new NpzError('zip', 'bad central directory entry')
    const nameLen = v.getUint16(p + 28, true)
    const extraLen = v.getUint16(p + 30, true)
    const commentLen = v.getUint16(p + 32, true)
    let usize = v.getUint32(p + 24, true)
    let csize = v.getUint32(p + 20, true)
    let local = v.getUint32(p + 42, true)
    // ZIP64 扩展字段（id 1）：只含主记录中为 0xFFFFFFFF 的字段，顺序为原始大小、压缩大小、本地头偏移
    let x = p + 46 + nameLen
    const xEnd = x + extraLen
    while (x + 4 <= xEnd) {
      const id = v.getUint16(x, true)
      const sz = v.getUint16(x + 2, true)
      if (id === 1) {
        let q = x + 4
        if (usize === U32_MAX) { usize = u64(v, q); q += 8 }
        if (csize === U32_MAX) { csize = u64(v, q); q += 8 }
        if (local === U32_MAX) local = u64(v, q)
      }
      x += 4 + sz
    }
    out.push({ name: dec.decode(new Uint8Array(buf, p + 46, nameLen)), method: v.getUint16(p + 10, true), crc: v.getUint32(p + 16, true), csize, usize, local })
    p = xEnd + commentLen
  }
  return out
}

async function inflateRaw(b: Uint8Array): Promise<Uint8Array> {
  const ds = new DecompressionStream('deflate-raw')
  const r = new Response(new Blob([b as BlobPart]).stream().pipeThrough(ds))
  return new Uint8Array(await r.arrayBuffer())
}

/** 一个成员的原始字节（已校验 CRC-32 与大小） */
export async function zipMember(buf: ArrayBuffer, e: ZipEntry): Promise<Uint8Array> {
  const v = new DataView(buf)
  if (e.local + 30 > buf.byteLength || v.getUint32(e.local, true) !== LOC) throw new NpzError('zip', `bad local header for ${e.name}`)
  const start = e.local + 30 + v.getUint16(e.local + 26, true) + v.getUint16(e.local + 28, true)
  if (start + e.csize > buf.byteLength) throw new NpzError('size', `${e.name} runs past the end of the archive`)
  const raw = new Uint8Array(buf, start, e.csize)
  let b: Uint8Array
  if (e.method === 0) b = raw
  else if (e.method === 8) b = await inflateRaw(raw)
  else throw new NpzError('method', `${e.name}: compression method ${e.method}`)
  if (b.length !== e.usize) throw new NpzError('size', `${e.name}: ${b.length} bytes, expected ${e.usize}`)
  if (crc32(b) !== e.crc) throw new NpzError('crc', `${e.name}: CRC-32 mismatch`)
  return b
}

const DTYPES: Record<string, { dtype: NpyDtype; size: number }> = {
  '<f8': { dtype: 'f8', size: 8 }, '<f4': { dtype: 'f4', size: 4 }, '<i8': { dtype: 'i8', size: 8 }, '<i4': { dtype: 'i4', size: 4 },
  '<i2': { dtype: 'i2', size: 2 }, '|u1': { dtype: 'u1', size: 1 }, '<u1': { dtype: 'u1', size: 1 },
  '<u2': { dtype: 'u2', size: 2 }, '<u4': { dtype: 'u4', size: 4 }, '<u8': { dtype: 'u8', size: 8 },
}
const MAGIC = [0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59]                       // "\x93NUMPY"

/** `.npy` 解码：数据复制到新的对齐缓冲区（ZIP 中的成员偏移不保证 8 字节对齐） */
export function parseNpy(b: Uint8Array): NpyArray {
  if (b.length < 10 || MAGIC.some((m, i) => b[i] !== m)) throw new NpzError('npy', 'not an .npy array')
  const major = b[6]!
  const v = new DataView(b.buffer, b.byteOffset, b.byteLength)
  const hl = major === 1 ? v.getUint16(8, true) : v.getUint32(8, true)
  const h0 = major === 1 ? 10 : 12
  if (major < 1 || major > 3 || h0 + hl > b.length) throw new NpzError('npy', `unsupported .npy version ${major}`)
  const header = new TextDecoder(major === 3 ? 'utf-8' : 'latin1').decode(b.subarray(h0, h0 + hl))
  const descr = /'descr'\s*:\s*'([^']+)'/.exec(header)?.[1]
  const fortran = /'fortran_order'\s*:\s*(True|False)/.exec(header)?.[1]
  const shapeText = /'shape'\s*:\s*\(([^)]*)\)/.exec(header)?.[1]
  if (descr === undefined || fortran === undefined || shapeText === undefined) throw new NpzError('npy', 'malformed .npy header')
  const dt = DTYPES[descr]
  if (!dt) throw new NpzError('dtype', `dtype ${descr}`)
  if (fortran === 'True') throw new NpzError('order', 'Fortran order')
  const shape = shapeText.split(',').map((s) => s.trim()).filter(Boolean).map(Number)
  if (shape.some((n) => !Number.isInteger(n) || n < 0)) throw new NpzError('npy', `shape (${shapeText})`)
  const count = shape.reduce((a, n) => a * n, 1)
  const off = h0 + hl
  if (off + count * dt.size > b.length) throw new NpzError('size', `${count} × ${dt.size} bytes past the end`)
  const ab = b.slice(off, off + count * dt.size).buffer
  const data: NpyData = dt.dtype === 'f8' ? new Float64Array(ab) : dt.dtype === 'f4' ? new Float32Array(ab) : dt.dtype === 'i8' ? new BigInt64Array(ab)
    : dt.dtype === 'i4' ? new Int32Array(ab) : dt.dtype === 'i2' ? new Int16Array(ab) : dt.dtype === 'u2' ? new Uint16Array(ab)
      : dt.dtype === 'u4' ? new Uint32Array(ab) : dt.dtype === 'u8' ? new BigUint64Array(ab) : new Uint8Array(ab)
  return { dtype: dt.dtype, shape, data }
}

/** `.npz` 解码：成员名去掉 `.npy` 后缀作为键（与 `np.load(...)[key]` 相同） */
export async function readNpz(buf: ArrayBuffer): Promise<Map<string, NpyArray>> {
  const out = new Map<string, NpyArray>()
  for (const e of zipEntries(buf)) {
    if (e.name.endsWith('/')) continue
    out.set(e.name.replace(/\.npy$/, ''), parseNpy(await zipMember(buf, e)))
  }
  return out
}
