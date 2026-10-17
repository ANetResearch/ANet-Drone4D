// Evaluation-view truth overlay (AWR-14 §21.7.3 "评测视图叠加"; AWR-17 §18.8 BN-52 with decode=raw). Container
// `awr.sensor.camera_gt.v1`: "D3GT" | version u16 | n_blocks u16 | json_len u32 | frame.v1 JSON | n × (name_len u16 | name |
// len u32 | bytes), little-endian; with decode=raw every truth block is a plain array (codec "raw"). The overlay marks
// instance boundaries (lieflat data role) and hatches invalid_mask bit 0 (void below horizon, faint role); object boxes
// and labels are drawn by the tile on a 2D context. No red (AWR-15).
export interface TruthObject {
  inst: number; actor: string; class: number; visible_ratio: number; h_px_unocc: number; h_px_visible: number
  bbox_xywh: [number, number, number, number]; occluder_sem_px: Record<string, number>
}
interface BlockRef { ref: string; codec?: string; dtype?: string }
export interface TruthRecord {
  frame_id: string
  agent_view: { width: number; height: number }
  ground_truth: { instance?: BlockRef; invalid_mask?: BlockRef; semantic?: BlockRef; objects?: TruthObject[] }
}
export interface TruthFrame { rec: TruthRecord; blocks: Map<string, Uint8Array> }

export function parseD3GT(buf: ArrayBuffer): TruthFrame {
  const u8 = new Uint8Array(buf)
  const dv = new DataView(buf)
  if (u8.length < 12 || String.fromCharCode(u8[0]!, u8[1]!, u8[2]!, u8[3]!) !== 'D3GT') throw new Error('not a camera_gt.v1 container')
  if (dv.getUint16(4, true) !== 1) throw new Error('unsupported camera_gt version')
  const n = dv.getUint16(6, true)
  const jl = dv.getUint32(8, true)
  const td = new TextDecoder()
  const rec = JSON.parse(td.decode(u8.subarray(12, 12 + jl))) as TruthRecord
  const blocks = new Map<string, Uint8Array>()
  let p = 12 + jl
  for (let i = 0; i < n; i++) {
    const nl = dv.getUint16(p, true)
    const name = td.decode(u8.subarray(p + 2, p + 2 + nl))
    p += 2 + nl
    const ln = dv.getUint32(p, true)
    if (p + 4 + ln > u8.length) throw new Error('truncated camera_gt block')
    blocks.set(name, u8.subarray(p + 4, p + 4 + ln))
    p += 4 + ln
  }
  return { rec, blocks }
}

function rawBlock(fr: TruthFrame, ref: BlockRef | undefined, bytesPer: number): Uint8Array | null {
  if (!ref || (ref.codec !== undefined && ref.codec !== 'raw')) return null
  const b = fr.blocks.get(ref.ref)
  const { width, height } = fr.rec.agent_view
  return b && b.length === width * height * bytesPer ? b : null
}

export type Rgb = readonly [number, number, number]

/** RGBA overlay at truth resolution; null when the frame carries neither instance nor invalid_mask as raw blocks */
export function truthOverlay(fr: TruthFrame, edge: Rgb, hatch: Rgb): Uint8ClampedArray<ArrayBuffer> | null {
  const { width: w, height: h } = fr.rec.agent_view
  const ib = rawBlock(fr, fr.rec.ground_truth.instance, 2)
  const mb = rawBlock(fr, fr.rec.ground_truth.invalid_mask, 1)
  if (!ib && !mb) return null
  const inst = ib ? new Uint16Array(ib.slice().buffer) : null
  const px = new Uint8ClampedArray(w * h * 4)
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x
      const o = i * 4
      const id = inst ? inst[i]! : 0
      if (id && ((x + 1 < w && inst![i + 1] !== id) || (y + 1 < h && inst![i + w] !== id) || x === 0 || y === 0 || inst![i - 1] !== id
        || inst![i - w] !== id)) {
        px[o] = edge[0]; px[o + 1] = edge[1]; px[o + 2] = edge[2]; px[o + 3] = 255
      } else if (mb && (mb[i]! & 1) && ((x + y) & 7) === 0) {
        px[o] = hatch[0]; px[o + 1] = hatch[1]; px[o + 2] = hatch[2]; px[o + 3] = 160
      }
    }
  }
  return px
}
