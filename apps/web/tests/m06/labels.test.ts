// M06-AC-047 layout part (M06 §6.13; r14 §3.8): labels never overlap on the 96 x 28 grid (2 x 1 cells), the cap holds
// (S 16, B/A 48, governor 8 / 4), priority selected > red > critical > warning > hover > GoTo > other then distance;
// the LabelLayer writes transforms only when a label moved >= 0.5 px and text at <= 4 Hz on Tier S.
import { describe, expect, it } from 'vitest'
import { Declutter, DECLUTTER, LABEL_PRIO } from '@/engine'

describe('label declutter (M06-AC-047)', () => {
  it('no overlap, cap and priority order', () => {
    const d = new Declutter(64)
    const n = 60
    const sx = new Float32Array(64)
    const sy = new Float32Array(64)
    const prio = new Uint8Array(64)
    const dist = new Float32Array(64)
    let s = 3
    const rnd = (): number => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296
    for (let i = 0; i < n; i++) {
      sx[i] = rnd() * 1280
      sy[i] = rnd() * 720
      prio[i] = 1 + Math.floor(rnd() * 7)
      dist[i] = rnd() * 1000
    }
    sx[0] = 300; sy[0] = 300; prio[0] = LABEL_PRIO.hover
    sx[1] = 310; sy[1] = 305; prio[1] = LABEL_PRIO.selected // same cell: the selected label wins
    const out = new Int32Array(48)
    const k = d.place(n, sx, sy, prio, dist, 1280, 720, DECLUTTER.capS, out)
    expect(k).toBeLessThanOrEqual(16)
    const acc = Array.from(out.slice(0, k))
    expect(acc).toContain(1)
    expect(acc).not.toContain(0)
    // no two accepted labels share a cell pair
    const cells = new Set<string>()
    for (const i of acc) {
      const c0 = Math.floor(sx[i] / 96 - 0.5)
      const r = Math.floor(sy[i] / 28)
      for (const c of [c0, c0 + 1]) {
        const key = `${c},${r}`
        expect(cells.has(key)).toBe(false)
        cells.add(key)
      }
    }
    // accepted in non-increasing priority
    for (let j = 1; j < acc.length; j++) expect(prio[acc[j]]).toBeLessThanOrEqual(prio[acc[j - 1]])
    expect(d.place(n, sx, sy, prio, dist, 1280, 720, 4, out)).toBeLessThanOrEqual(4)
  })
})
