// Observation wall logic (AWR-14 §21.7.2-§21.7.4; AWR-17 §18.8.2).
import { describe, expect, it } from 'vitest'
import { depthToRgba } from '@/ui/lab/wall/depth'
import { frameAt, isStale, parseD3Frames } from '@/ui/lab/wall/frames'
import { layoutWall } from '@/ui/lab/wall/layout'
import { usePlayhead } from '@/stores/lab/playhead'

function container(frames: { seq: number; t: number; data: number[] }[]): ArrayBuffer {
  const head = new TextEncoder().encode(JSON.stringify({ n: frames.length }))
  const size = 12 + head.length + frames.reduce((a, f) => a + 48 + f.data.length, 0) + 32
  const buf = new ArrayBuffer(size)
  const dv = new DataView(buf)
  const u8 = new Uint8Array(buf)
  u8.set([68, 51, 70, 82], 0)
  dv.setUint16(4, 1, true)
  dv.setUint32(8, head.length, true)
  u8.set(head, 12)
  let p = 12 + head.length
  for (const f of frames) {
    dv.setUint32(p, f.seq, true)
    dv.setBigInt64(p + 4, BigInt(f.t), true)
    dv.setUint32(p + 12, f.data.length, true)
    u8.set(f.data, p + 48)
    p += 48 + f.data.length
  }
  return buf
}

describe('lab wall', () => {
  it('lays out the smallest grid that holds n tiles at 16:9', () => {
    const l = layoutWall(3, 1204, 900)
    expect(l.cap).toBe(4)
    expect(l.cols).toBe(2)
    expect(l.tileW).toBe(600)
    expect(l.tileH).toBe(338)
    expect(layoutWall(17, 1000, 600).pages).toBe(2)
    expect(layoutWall(3, 1000, 600, 1).pages).toBe(3)
  })

  it('picks the last frame at or before the playhead and flags stale tiles', () => {
    const items = [{ seq: 1, t_ns: 96e6 }, { seq: 2, t_ns: 196e6 }, { seq: 3, t_ns: 296e6 }]
    expect(frameAt(items, 50e6)).toBe(-1)
    expect(frameAt(items, 196e6)).toBe(1)
    expect(frameAt(items, 250e6)).toBe(1)
    expect(frameAt(items, 9e9)).toBe(2)
    expect(isStale(items[2], 296e6 + 140e6, 100e6)).toBe(false)
    expect(isStale(items[2], 296e6 + 160e6, 100e6)).toBe(true)
  })

  it('parses d3frames.v1 containers', () => {
    const { head, frames } = parseD3Frames(container([{ seq: 4, t: 396_000_000, data: [255, 216, 1] }, { seq: 7, t: 696_000_000, data: [9] }]))
    expect(head.n).toBe(2)
    expect(frames.map((f) => f.seq)).toEqual([4, 7])
    expect(frames[0]!.tNs).toBe(396_000_000)
    expect(Array.from(frames[0]!.data)).toEqual([255, 216, 1])
    expect(() => parseD3Frames(new ArrayBuffer(64))).toThrow()
  })

  it('maps depth to a log grey ramp with a neutral for no geometry', () => {
    const px = depthToRgba(new Float32Array([0, 0.3, 60, 4.2426, Number.NaN]))
    expect([px[0], px[4], px[8]]).toEqual([24, 255, 0])
    expect(px[12]).toBeGreaterThan(110)
    expect(px[12]).toBeLessThan(145)
    expect(px[16]).toBe(24)
    expect(px[19]).toBe(255)
  })

  it('advances the playhead and stops at the end', () => {
    const s = usePlayhead.getState()
    s.setDuration(10)
    s.seek(9.5)
    s.play()
    usePlayhead.getState().advance(1)
    expect(usePlayhead.getState().tS).toBe(10)
    expect(usePlayhead.getState().playing).toBe(false)
  })
})

describe('lab login next', () => {
  it('accepts only relative /lab paths', async () => {
    const { safeNext } = await import('@/ui/lab/LabLogin')
    expect(safeNext('/lab/runs/cap-0001?t=3')).toBe('/lab/runs/cap-0001?t=3')
    for (const bad of [null, '', '//evil.example/lab', 'https://evil.example/lab', '/worlds', '/lab\\..\\x', '/labx']) expect(safeNext(bad)).toBe('/lab/runs')
  })
})

describe('truth overlay (camera_gt.v1, decode=raw)', () => {
  const pack = (rec: object, blocks: [string, Uint8Array][]): ArrayBuffer => {
    const enc = new TextEncoder()
    const js = enc.encode(JSON.stringify(rec))
    const parts: Uint8Array[] = [enc.encode('D3GT'), new Uint8Array(new Uint16Array([1, blocks.length]).buffer), new Uint8Array(new Uint32Array([js.length]).buffer), js]
    for (const [name, b] of blocks) {
      const nb = enc.encode(name)
      parts.push(new Uint8Array(new Uint16Array([nb.length]).buffer), nb, new Uint8Array(new Uint32Array([b.length]).buffer), b)
    }
    const out = new Uint8Array(parts.reduce((a, p) => a + p.length, 0))
    let o = 0
    for (const p of parts) { out.set(p, o); o += p.length }
    return out.buffer
  }

  it('parses blocks and outlines instances, hatches invalid pixels', async () => {
    const { parseD3GT, truthOverlay } = await import('@/ui/lab/wall/truth')
    const w = 8
    const h = 8
    const inst = new Uint16Array(w * h)
    for (let y = 2; y < 6; y++) for (let x = 2; x < 6; x++) inst[y * w + x] = 7
    const mask = new Uint8Array(w * h).fill(1)
    const rec = { frame_id: 'r/u/c/1', agent_view: { width: w, height: h },
      ground_truth: { instance: { ref: 'i', codec: 'raw' }, invalid_mask: { ref: 'm', codec: 'raw' }, objects: [] } }
    const fr = parseD3GT(pack(rec, [['i', new Uint8Array(inst.buffer)], ['m', mask]]))
    expect(fr.rec.frame_id).toBe('r/u/c/1')
    const px = truthOverlay(fr, [250, 250, 250], [90, 90, 90])!
    const at = (x: number, y: number) => Array.from(px.subarray((y * w + x) * 4, (y * w + x) * 4 + 4))
    expect(at(2, 2)).toEqual([250, 250, 250, 255])                  // instance boundary
    expect(at(3, 3)[3]).toBe(0)                                      // instance interior stays clear
    expect(at(0, 0)).toEqual([90, 90, 90, 160])                     // hatch on (x + y) % 8 == 0
    expect(at(1, 0)[3]).toBe(0)
    const zipped = parseD3GT(pack({ ...rec, ground_truth: { instance: { ref: 'i', codec: 'zstd' } } }, [['i', new Uint8Array(3)]]))
    expect(truthOverlay(zipped, [0, 0, 0], [0, 0, 0])).toBeNull()      // compressed blocks are not decoded in the browser
  })
})
