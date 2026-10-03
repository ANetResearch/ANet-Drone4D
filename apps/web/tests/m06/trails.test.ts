// M06-AC-033 CPU and batch parts (M06 §6.10, FR-042, FR-043): 256-sample rings appended every 0.5 s or 5 m, epoch
// clears all, RESET clears only the producer's range, block rebase after 2 h; GPU batches copy the CPU history into
// slots, append new samples incrementally, recolour on red-owner changes and follow the PerfGovernor limits
// (16 x 256 -> 8 x 128 -> 4 x 64 -> selected only). Rendering (3 draws, alpha by age) is covered by the browser spec.
import { QL_STRIDE, QL_VPS } from '@/engine/lines/quadLines'
import { describe, expect, it } from 'vitest'
import { TRAIL, TrailBatch, TrailRing, TRAIL_STYLES } from '@/engine'

describe('TrailRing (M06-FR-042, FR-043)', () => {
  it('appends on 0.5 s or 5 m and wraps at 256 samples', () => {
    const r = new TrailRing(8)
    expect(r.append(1, 0, 0, 0, 100)).toBe(true)
    expect(r.append(1, 1, 0, 0, 100.2)).toBe(false) // 0.2 s and 1 m
    expect(r.append(1, 6, 0, 0, 100.3)).toBe(true) // 6 m
    expect(r.append(1, 6, 0, 0, 100.8)).toBe(true) // 0.5 s
    for (let k = 0; k < 400; k++) r.append(1, 0, 0, 0, 101 + k)
    const row = r.rowFor(1)
    expect(r.count[row]).toBe(TRAIL.samples)
    const s = new Float32Array(4)
    r.sample(row, TRAIL.samples - 1, s)
    expect(s[3]).toBeCloseTo(101 + 399 - 100, 3) // relative to the block start (first sample)
  })

  it('epoch clears everything; RESET clears only its producer range; removal recycles rows', () => {
    const r = new TrailRing(8)
    for (const a of [1, 2, 100, 101]) r.append(a, 0, 0, 0, 10)
    r.clearRange(100, 50)
    expect([r.rowFor(1) >= 0, r.rowFor(2) >= 0, r.rowFor(100), r.rowFor(101)]).toEqual([true, true, -1, -1])
    r.clear(2)
    expect(r.rowFor(2)).toBe(-1)
    r.clearAll()
    expect(r.rowFor(1)).toBe(-1)
    for (let a = 0; a < 8; a++) expect(r.append(200 + a, 0, 0, 0, 1)).toBe(true)
    expect(r.append(300, 0, 0, 0, 1)).toBe(false) // capacity
  })

  it('keeps the row when the same instant repeats (paused clock, Float32 round-up); restarts on a real seek back', () => {
    const r = new TrailRing(2)
    r.append(1, 0, 0, 0, 6.44) // block start
    // 108.104 - 6.44 = 101.664 is stored as 101.66400146484375 (Float32 rounds up): the old dt < 0 test restarted the row
    // on every frame rendered at this paused instant
    for (let k = 0; k < 6; k++) r.append(1, 10, 0, 0, 7 + k)
    r.append(1, 20, 0, 0, 108.104)
    const row = r.rowFor(1)
    const before = r.count[row]
    for (let k = 0; k < 10; k++) expect(r.append(1, 20, 0, 0, 108.104)).toBe(false)
    expect(r.count[row]).toBe(before)
    expect(r.append(1, 25, 0, 0, 108.604)).toBe(true) // playback resumes
    expect(r.count[row]).toBe(before + 1)
    expect(r.append(1, 0, 0, 0, 100)).toBe(true) // seek back by 8.6 s: history restarts
    expect(r.count[row]).toBe(1)
  })

  it('rebases the block after 2 h and bumps rebaseCount', () => {
    const r = new TrailRing(2)
    r.append(1, 0, 0, 0, 0)
    r.append(1, 10, 0, 0, 7300)
    expect(r.rebaseCount).toBe(1)
    const s = new Float32Array(4)
    r.sample(r.rowFor(1), 1, s)
    expect(s[3]).toBeCloseTo(TRAIL.windowS, 3)
  })
})

describe('TrailBatch (M06-AC-033)', () => {
  it('copies history, appends incrementally, recolours and obeys the limits', () => {
    const ring = new TrailRing(8)
    for (let k = 0; k < 10; k++) ring.append(3, k * 10, 0, 0, k)
    const b = new TrailBatch(16, 256, TRAIL_STYLES.focus)
    b.assign(0, 3, 0, ring)
    // quad-line layout (FX2-R3): 4 vertices per segment, stride 9 floats (start, end, time start, time end, palette)
    const geo = b.mesh.geometry as unknown as { attributes: Record<string, { data: { array: Float32Array } }>; drawRange: { count: number } }
    const d = geo.attributes.qlExtra.data.array
    const seg = (i: number, k: number): number => d[(i * QL_VPS) * QL_STRIDE + k]
    let valid = 0
    for (let i = 0; i < 256; i++) if (seg(i, 6) >= 0) valid++
    expect(valid).toBe(9) // 10 samples -> 9 segments
    ring.append(3, 200, 0, 0, 20)
    b.sync(ring, 20, () => 1)
    valid = 0
    for (let i = 0; i < 256; i++) if (seg(i, 6) >= 0) valid++
    expect(valid).toBe(10)
    expect(seg(0, 8)).toBe(1) // recoloured (red owner)
    // all 4 vertices of a segment carry the same data; the draw range ends at the last occupied slot
    expect(d[(0 * QL_VPS + 3) * QL_STRIDE + 8]).toBe(1)
    expect(geo.drawRange.count).toBe(256 * 6)
    expect(b.mesh.visible).toBe(true)
    b.setLimits(8, 128, ring)
    expect([b.activeSlots, b.segs]).toEqual([8, 128])
    b.setLimits(0, 64, ring)
    b.sync(ring, 20, () => 0)
    expect(b.mesh.visible).toBe(false)
    expect(b.drawCount()).toBe(0)
  })
})
