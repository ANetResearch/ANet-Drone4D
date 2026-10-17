// Frame selection and the d3frames.v1 range container (AWR-14 §21.7.3, §21.7.4; AWR-17 §18.8.2). The tile shows the
// frame with the largest t_mid <= playhead (never a future frame); stale when the playhead is more than 1.5 frame periods
// past it. Container: "D3FR" | version u16 | flags u16 | header_len u32 | header JSON | records (seq u32, t_ns i64,
// len u32, sha256 32 B, payload) | trailer sha256 (32 B); little-endian.
export interface FrameRef { seq: number; t_ns: number }
export interface RangeFrame { seq: number; tNs: number; data: Uint8Array }

/** index of the last item with t_ns <= tNs, or -1 (items ascending by t_ns) */
export function frameAt(items: readonly FrameRef[], tNs: number): number {
  let lo = 0
  let hi = items.length - 1
  let best = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (items[mid]!.t_ns <= tNs) {
      best = mid
      lo = mid + 1
    } else hi = mid - 1
  }
  return best
}

export function isStale(item: FrameRef | undefined, tNs: number, periodNs: number): boolean {
  return item !== undefined && tNs - item.t_ns > 1.5 * periodNs
}

export function parseD3Frames(buf: ArrayBuffer): { head: Record<string, unknown>; frames: RangeFrame[] } {
  const u8 = new Uint8Array(buf)
  const dv = new DataView(buf)
  if (u8.length < 44 || String.fromCharCode(u8[0]!, u8[1]!, u8[2]!, u8[3]!) !== 'D3FR') throw new Error('not a d3frames.v1 container')
  if (dv.getUint16(4, true) !== 1) throw new Error('unsupported d3frames version')
  const hl = dv.getUint32(8, true)
  const head = JSON.parse(new TextDecoder().decode(u8.subarray(12, 12 + hl))) as Record<string, unknown>
  const frames: RangeFrame[] = []
  let p = 12 + hl
  const end = u8.length - 32
  while (p < end) {
    const seq = dv.getUint32(p, true)
    const tNs = Number(dv.getBigInt64(p + 4, true))
    const n = dv.getUint32(p + 12, true)
    if (p + 48 + n > end) throw new Error('truncated d3frames record')
    frames.push({ seq, tNs, data: u8.subarray(p + 48, p + 48 + n) })
    p += 48 + n
  }
  return { head, frames }
}
