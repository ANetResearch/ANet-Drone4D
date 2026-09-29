// Quality coverage mask packing (AWR-18 §4.5 item 2; M05 request 5): alpha > 0 -> bit set, 1 bit per pixel, rows
// bottom-up as read back, LSB first.
import { describe, expect, it } from 'vitest'
import { packCoverage } from '@/viewport/qualityMask'

describe('quality coverage mask', () => {
  it('packs alpha coverage LSB first', () => {
    const w = 5
    const h = 2
    const rgba = new Uint8Array(w * h * 4)
    for (const i of [0, 3, 7, 9]) rgba[4 * i + 3] = 255
    rgba[4 * 1] = 200 // colour without alpha does not count
    const m = packCoverage(rgba, w, h)
    expect(m.length).toBe(2)
    expect(m[0]).toBe(0b10001001)
    expect(m[1]).toBe(0b00000010)
  })
})
