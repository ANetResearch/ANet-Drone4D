// hierarchy.bin and hierarchy_ext.bin parsing (AWR-16 §4.2, §4.8; Potree 2.0 OctreeLoader.parseHierarchy order;
// M05-FR-001, FR-019). Owner: M05.
// 22-byte records `<BBIqq`: type (0 NORMAL, 1 LEAF, 2 PROXY), childMask, numPoints (own points), byteOffset, byteSize.
// A chunk is breadth first: the children of record i are appended in child order when i is visited; a PROXY record
// spawns no children, its byteOffset/byteSize address the sub-chunk in hierarchy.bin whose first record is the same
// node. The six D1 cities are single-chunk; paged hierarchies (FR-019, synthetic fixtures) are expanded at open time
// with one Range per sub-chunk (and the mirrored hierarchy_ext.bin range [o / 22 x 12, s / 22 x 12)).
import type { HierRecord } from '../core/NodeStore'
import { PC_FORMAT_UNSUPPORTED, PcError } from './meta'

export const HIER_RECORD = 22
export const HIER_EXT_RECORD = 12
export const T_NORMAL = 0
export const T_LEAF = 1
export const T_PROXY = 2

const u64 = (dv: DataView, off: number): number => dv.getUint32(off, true) + dv.getUint32(off + 4, true) * 4294967296

interface ChunkRoot { parent: number; child: number; level: number; name: string }

/** parse one chunk; records of the chunk root's subtree in BFS order, PROXY records kept (no children) */
export function parseChunk(buf: ArrayBuffer, byteOffset: number, byteLength: number, root: ChunkRoot = { parent: -1, child: 0, level: 0, name: 'r' }): HierRecord[] {
  const dv = new DataView(buf, byteOffset, byteLength)
  const n = Math.floor(byteLength / HIER_RECORD)
  if (n === 0) throw new PcError(PC_FORMAT_UNSUPPORTED, 'hierarchy chunk is empty')
  const out: HierRecord[] = [{ type: 0, childMask: 0, numPoints: 0, byteOffset: 0, byteSize: 0, parent: root.parent, child: root.child, level: root.level, name: root.name }]
  for (let i = 0; i < out.length; i++) {
    if (i >= n) throw new PcError(PC_FORMAT_UNSUPPORTED, `hierarchy chunk ends at record ${n}, node ${out[i].name} missing`)
    const o = i * HIER_RECORD
    const rec = out[i]
    rec.type = dv.getUint8(o)
    rec.childMask = dv.getUint8(o + 1)
    rec.numPoints = dv.getUint32(o + 2, true)
    rec.byteOffset = u64(dv, o + 6)
    rec.byteSize = u64(dv, o + 14)
    if (rec.type === T_PROXY) continue
    for (let c = 0; c < 8; c++) {
      if ((rec.childMask >> c) & 1) out.push({ type: 0, childMask: 0, numPoints: 0, byteOffset: 0, byteSize: 0, parent: i, child: c, level: rec.level + 1, name: rec.name + c })
    }
  }
  if (out.length !== n) throw new PcError(PC_FORMAT_UNSUPPORTED, `hierarchy chunk has ${n} records, the tree has ${out.length} nodes`)
  return out
}

/** single-chunk hierarchy (the D1 cities); a PROXY record is an error here, use parseHierarchyPaged */
export function parseHierarchy(buf: ArrayBuffer, byteLength = buf.byteLength): HierRecord[] {
  const out = parseChunk(buf, 0, byteLength)
  for (const r of out) if (r.type === T_PROXY) throw new PcError(PC_FORMAT_UNSUPPORTED, 'paged hierarchy: use parseHierarchyPaged')
  return out
}

export interface PagedHierarchy { records: HierRecord[]; ext: Uint16Array | null }

/**
 * Paged hierarchy (FR-019): the first chunk plus every PROXY sub-chunk, loaded through loadChunk(offset, size) and
 * loadExt(offset, size) (Range requests), expanded into one record list whose parents precede their children.
 */
export async function parseHierarchyPaged(first: ArrayBuffer, firstExt: ArrayBuffer | null,
  loadChunk: (offset: number, size: number) => Promise<ArrayBuffer>, loadExt: ((offset: number, size: number) => Promise<ArrayBuffer | null>) | null): Promise<PagedHierarchy> {
  const records = parseChunk(first, 0, first.byteLength)
  const ext: number[][] = []
  const pushExt = (b: ArrayBuffer | null, count: number, recIndex: (k: number) => number): void => {
    for (let k = 0; k < count; k++) {
      const r = recIndex(k)
      if (!b || b.byteLength < (k + 1) * HIER_EXT_RECORD) {
        ext[r] = [0, 0, 0, 65535, 65535, 65535]
        continue
      }
      const dv = new DataView(b, k * HIER_EXT_RECORD, HIER_EXT_RECORD)
      ext[r] = [0, 2, 4, 6, 8, 10].map((o) => dv.getUint16(o, true))
    }
  }
  pushExt(firstExt, records.length, (k) => k)
  for (let i = 0; i < records.length; i++) {
    const p = records[i]
    if (p.type !== T_PROXY) continue
    const off = p.byteOffset
    const size = p.byteSize
    const [cb, xb] = await Promise.all([loadChunk(off, size), loadExt ? loadExt((off / HIER_RECORD) * HIER_EXT_RECORD, (size / HIER_RECORD) * HIER_EXT_RECORD) : Promise.resolve(null)])
    const sub = parseChunk(cb, 0, cb.byteLength, { parent: p.parent, child: p.child, level: p.level, name: p.name })
    const base = records.length
    // the sub-chunk's first record is the proxy node itself; the rest are appended with remapped parents
    const head = sub[0]
    p.type = head.type
    p.childMask = head.childMask
    p.numPoints = head.numPoints
    p.byteOffset = head.byteOffset
    p.byteSize = head.byteSize
    for (let k = 1; k < sub.length; k++) {
      const r = sub[k]
      records.push({ ...r, parent: r.parent === 0 ? i : base + r.parent - 1 })
    }
    pushExt(xb, sub.length, (k) => (k === 0 ? i : base + k - 1))
  }
  let out: Uint16Array | null = null
  if (firstExt) {
    out = new Uint16Array(6 * records.length)
    for (let k = 0; k < records.length; k++) out.set(ext[k] ?? [0, 0, 0, 65535, 65535, 65535], 6 * k)
  }
  return { records, ext: out }
}

/** hierarchy_ext.bin: 6 x u16 per record (subtree tight AABB normalised to the node cube), mirrored with hierarchy.bin */
export function parseHierarchyExt(buf: ArrayBuffer, n: number): Uint16Array | null {
  if (buf.byteLength < n * HIER_EXT_RECORD) return null
  const out = new Uint16Array(6 * n)
  const dv = new DataView(buf)
  for (let i = 0; i < 6 * n; i++) out[i] = dv.getUint16(2 * i, true)
  return out
}
