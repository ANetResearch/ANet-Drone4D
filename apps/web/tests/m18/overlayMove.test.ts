// M18-FR-024, NFR-005 (D2-AC-30; D2-FX-R2-web round 2): the target overlay places shared canonical body meshes (keyed by
// shape, not by target or heading) and re-uploads positions only when targets just move. Same output as building each body
// at its heading (bodyMesh), no local mesh per walker, line and face info untouched by a move.
import { describe, expect, it } from 'vitest'
import type { BufferAttribute } from 'three'
import { TARGET_LITE32 as L } from '@awr/contracts/layouts'
import { TargetsModel, TargetsOverlay } from '@/engine/targets'
import { bodyMesh, newMeshBuffers } from '@/engine/targets/bodyMesh'

const SENS = [
  { idx: 0, kind: 'visual' as const, shape: 'sector' as const, radius_cfg_m: 60, enabled: true, z_min_m: 0, z_max_m: 60, half_angle_deg: 60, azimuth_offset_deg: 15 },
  { idx: 1, kind: 'acoustic' as const, shape: 'hemisphere' as const, radius_cfg_m: 40, enabled: true, z_min_m: 0, z_max_m: 40, half_angle_deg: 180, azimuth_offset_deg: 0 },
]

function world(n: number): { m: TargetsModel; set(k: number, x: number, y: number, z: number, h: number): void; push(): void } {
  const m = new TargetsModel()
  m.resolver = { targetIdOf: (no) => `tg-${no}` }
  const b = new Uint8Array(n * 32)
  const dv = new DataView(b.buffer)
  for (let k = 0; k < n; k++) {
    const o = k * 32
    dv.setUint16(o + L.AGENT_NO, k, true)
    dv.setUint8(o + L.CLASS, 1)
    dv.setUint8(o + L.STATE, 0x80 | 0x10)
    dv.setUint8(o + L.N_VOL, 2)
  }
  for (let k = 0; k < n; k++) {
    m.ingestDetail(`tg-${k}`, { id: `tg-${k}`, class: 'person', awareness: 'UNAWARE', alertness: 0, criterion_mode: 'geometric', discovered_count: 0, cfg_rev: 1, sensitivity: SENS })
  }
  return {
    m,
    set(k, x, y, z, h) {
      const o = k * 32
      dv.setFloat32(o + L.POS, x, true)
      dv.setFloat32(o + L.POS + 4, y, true)
      dv.setFloat32(o + L.POS + 8, z, true)
      dv.setFloat32(o + L.HEADING_RAD, h, true)
    },
    push() {
      m.ingestRows(b, 0)
    },
  }
}

const OPTS = { view: 'configured' as const, modelId: null, selectedId: null, redId: null, showBodies: true }
const f32 = (v: number) => Math.fround(v)

describe('target overlay: shared body meshes, move-only uploads (D2-AC-30)', () => {
  it('places the sector at the heading exactly as bodyMesh builds it', () => {
    const w = world(1)
    w.set(0, 12.5, -40.25, 7.5, 1.234)
    w.push()
    const o = new TargetsOverlay()
    o.update(w.m, OPTS)
    expect(o.nBodies).toBe(2)
    const ref = newMeshBuffers(1024, 256)
    // the reference: the configured sector at the heading, then the hemisphere, translated to the foot point
    bodyMesh({ kind: 'visual', shape: 'sector', radius_max_m: 60, z_min_m: 0, z_max_m: 60, half_angle_deg: 60, azimuth_offset_deg: 15 }, f32(1.234), false, ref)
    bodyMesh({ kind: 'acoustic', shape: 'hemisphere', radius_max_m: 40 }, 0, true, ref)
    const P = o.outlines.obj.geometry.getAttribute('position').array as Float32Array
    expect(o.outlines.n).toBe(ref.nSeg)
    let worst = 0
    const t = [12.5, -40.25, 7.5]
    for (let i = 0; i < ref.nSeg * 6; i++) worst = Math.max(worst, Math.abs(P[i] - (ref.seg[i] + t[i % 3])))
    expect(worst).toBeLessThan(2e-3)
    // the indexed faces expand to the same triangle list (same order and winding)
    const F = o.faces.geometry.getAttribute('position').array as Float32Array
    const X = o.faces.geometry.index!.array as Uint32Array
    expect(o.faces.geometry.drawRange.count).toBe(ref.nTri)
    let worstF = 0
    for (let i = 0; i < ref.nTri; i++) for (let c = 0; c < 3; c++) worstF = Math.max(worstF, Math.abs(F[3 * X[i] + c] - (ref.tri[3 * i + c] + t[c])))
    expect(worstF).toBeLessThan(2e-3)
  })

  it('30 turning walkers share 2 body meshes and 1 box; a move re-uploads positions only', () => {
    const N = 30
    const w = world(N)
    const st = Array.from({ length: N }, (_, k) => ({ x: 20 * k, y: -10 * k, z: 5 + (k % 4), h: 0.2 * k }))
    const step = (dh: number) => {
      for (let k = 0; k < N; k++) {
        const s = st[k]
        s.h += dh * (k % 2 ? 1 : -1)
        s.x += 0.14 * Math.cos(s.h)
        s.y += 0.14 * Math.sin(s.h)
        s.z += 0.013
        w.set(k, s.x, s.y, s.z, s.h)
      }
      w.push()
    }
    step(0)
    const o = new TargetsOverlay()
    expect(o.update(w.m, OPTS)).toBe(true)
    const local = (o as unknown as { local: Map<string, unknown>; boxMesh: Map<string, unknown> })
    const attr = (g: { getAttribute(n: string): unknown }, n: string) => g.getAttribute(n) as BufferAttribute
    const lineInfo = attr(o.outlines.obj.geometry, 'lineInfo')
    const linePos = attr(o.outlines.obj.geometry, 'position')
    const boxInfo = attr(o.boxes.obj.geometry, 'lineInfo')
    const faceInfo = attr(o.faces.geometry, 'bodyInfo')
    const facePos = attr(o.faces.geometry, 'position')
    const faceIdx = o.faces.geometry.index!
    const v0 = { li: lineInfo.version, lp: linePos.version, bi: boxInfo.version, fi: faceInfo.version, fp: facePos.version, fx: faceIdx.version }
    for (let i = 0; i < 20; i++) {
      step(0.05)
      expect(o.update(w.m, OPTS)).toBe(true)
    }
    expect(o.nBodies).toBe(60)
    expect(local.local.size).toBe(2)
    expect(local.boxMesh.size).toBe(1)
    expect(lineInfo.version).toBe(v0.li)
    expect(boxInfo.version).toBe(v0.bi)
    expect(faceInfo.version).toBe(v0.fi)
    expect(faceIdx.version).toBe(v0.fx)
    // 30 x (2 x 49 + 2 + 25 x 7) = 8 250 face vertices move, not 43 560 (non-indexed)
    expect(facePos.updateRanges[0].count).toBe(8250 * 3)
    expect(linePos.version).toBe(v0.lp + 20)
    expect(facePos.version).toBe(v0.fp + 20)
    // a colour change (selection) uploads the info again
    o.update(w.m, { ...OPTS, selectedId: 'tg-3' })
    expect(lineInfo.version).toBe(v0.li + 1)
    expect(boxInfo.version).toBe(v0.bi + 1)
    expect(faceInfo.version).toBe(v0.fi + 1)
  })
})
