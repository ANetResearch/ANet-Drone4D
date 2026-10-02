// Procedural low-poly multirotor (M06-FR-034; AWR-15 §10.4; ADR-022). Owner: M06.
// Hexacopter: body box, six prism arms, six flat rotor rings, built in FLU (x forward, y left, z up) and centred on the
// body. Vertex colours carry the part (body --drone-body, rear arms --drone-arm, front arms --drone-arm-front, rotor
// rings --drone-rotor; the brightest front arms show the nose by lightness, not hue). 's' (Tier S, lowpoly_s) stays
// <= 150 triangles, 'full' (Tier B/A, lowpoly) <= 300. Flat shading: non-indexed with face normals. The same attribute
// set {position, normal, color} for both detail levels keeps one shader program (M06 §6.4 rule 1). The hero geometry
// (placeholder for p600.glb) has {position, normal} only.
import { BufferAttribute, BufferGeometry } from 'three'
import { SCENE } from '@/lib/tokens/scene.gen'
import { hypot3 } from '../hypot'

export const LOWPOLY = { armM: 0.45, armW: 0.05, bodyW: 0.22, bodyH: 0.1, rotorR: 0.16, rotorW: 0.025, armsDeg: [30, 90, 150, 210, 270, 330] } as const

type V3 = readonly [number, number, number]
class Builder {
  readonly pos: number[] = []
  readonly col: number[] = []
  tri(a: V3, b: V3, c: V3, rgb: readonly number[]): void {
    this.pos.push(...a, ...b, ...c)
    for (let i = 0; i < 3; i++) this.col.push(rgb[0], rgb[1], rgb[2])
  }
  quad(a: V3, b: V3, c: V3, d: V3, rgb: readonly number[]): void {
    this.tri(a, b, c, rgb)
    this.tri(a, c, d, rgb)
  }
  box(cx: number, cy: number, cz: number, sx: number, sy: number, sz: number, rgb: readonly number[]): void {
    const x0 = cx - sx / 2, x1 = cx + sx / 2, y0 = cy - sy / 2, y1 = cy + sy / 2, z0 = cz - sz / 2, z1 = cz + sz / 2
    this.quad([x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1], rgb) // top
    this.quad([x0, y1, z0], [x1, y1, z0], [x1, y0, z0], [x0, y0, z0], rgb) // bottom
    this.quad([x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1], rgb)
    this.quad([x0, y1, z0], [x0, y0, z0], [x0, y0, z1], [x0, y1, z1], rgb)
    this.quad([x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1], rgb)
    this.quad([x1, y1, z0], [x0, y1, z0], [x0, y1, z1], [x1, y1, z1], rgb)
  }
  /** triangular prism along direction angle a (rad) from r0 to r1, width w, top at z */
  prism(a: number, r0: number, r1: number, w: number, z: number, rgb: readonly number[]): void {
    const c = Math.cos(a), s = Math.sin(a), px = -s * w / 2, py = c * w / 2
    const A0: V3 = [c * r0 + px, s * r0 + py, z], B0: V3 = [c * r0 - px, s * r0 - py, z], C0: V3 = [c * r0, s * r0, z - w]
    const A1: V3 = [c * r1 + px, s * r1 + py, z], B1: V3 = [c * r1 - px, s * r1 - py, z], C1: V3 = [c * r1, s * r1, z - w]
    this.quad(A0, A1, B1, B0, rgb) // top (z up)
    this.quad(B0, B1, C1, C0, rgb)
    this.quad(C0, C1, A1, A0, rgb)
    this.tri(A0, B0, C0, rgb)
    this.tri(A1, C1, B1, rgb)
  }
  /** flat annulus of n segments centred at (x, y, z), outer radius r, width w (both faces share one side, facing up) */
  ring(x: number, y: number, z: number, r: number, w: number, n: number, rgb: readonly number[]): void {
    for (let i = 0; i < n; i++) {
      const a0 = (2 * Math.PI * i) / n, a1 = (2 * Math.PI * (i + 1)) / n
      const o0: V3 = [x + r * Math.cos(a0), y + r * Math.sin(a0), z], o1: V3 = [x + r * Math.cos(a1), y + r * Math.sin(a1), z]
      const i0: V3 = [x + (r - w) * Math.cos(a0), y + (r - w) * Math.sin(a0), z], i1: V3 = [x + (r - w) * Math.cos(a1), y + (r - w) * Math.sin(a1), z]
      this.quad(i0, o0, o1, i1, rgb)
    }
  }
  build(withColor: boolean): BufferGeometry {
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(this.pos), 3))
    if (withColor) g.setAttribute('color', new BufferAttribute(new Float32Array(this.col), 3))
    g.computeVertexNormals()
    g.computeBoundingSphere()
    return g
  }
}

/** low-poly multirotor, 's' <= 150 triangles (lowpoly_s), 'full' <= 300 (lowpoly) */
export function makeLowPolyGeometry(detail: 's' | 'full' = 's'): BufferGeometry {
  const b = new Builder()
  const L = LOWPOLY
  b.box(0, 0, 0, L.bodyW * 1.2, L.bodyW, L.bodyH, SCENE.droneBody)
  for (const deg of L.armsDeg) {
    const a = (deg * Math.PI) / 180
    const front = Math.cos(a) > 0.5
    b.prism(a, L.bodyW * 0.45, L.armM, L.armW, L.bodyH * 0.3, front ? SCENE.droneArmFront : SCENE.droneArm)
    b.ring(Math.cos(a) * L.armM, Math.sin(a) * L.armM, L.bodyH * 0.45, L.rotorR, L.rotorW, detail === 's' ? 6 : 12, SCENE.droneRotor)
  }
  if (detail === 'full') b.box(0.02, 0, L.bodyH * 0.75, L.bodyW * 0.6, L.bodyW * 0.5, L.bodyH * 0.5, SCENE.droneBody)
  return b.build(true)
}

/** hero placeholder: the same silhouette with finer rotors, {position, normal} (p600.glb replaces it when served) */
export function makeHeroPlaceholderGeometry(): BufferGeometry {
  const b = new Builder()
  const L = LOWPOLY
  b.box(0, 0, 0, L.bodyW * 1.2, L.bodyW, L.bodyH, SCENE.droneBody)
  b.box(0.02, 0, L.bodyH * 0.75, L.bodyW * 0.6, L.bodyW * 0.5, L.bodyH * 0.5, SCENE.droneBody)
  for (const deg of L.armsDeg) {
    const a = (deg * Math.PI) / 180
    b.prism(a, L.bodyW * 0.45, L.armM, L.armW, L.bodyH * 0.3, SCENE.droneArm)
    b.ring(Math.cos(a) * L.armM, Math.sin(a) * L.armM, L.bodyH * 0.45, L.rotorR, L.rotorW, 24, SCENE.droneRotor)
    b.box(Math.cos(a) * L.armM, Math.sin(a) * L.armM, L.bodyH * 0.3, 0.05, 0.05, 0.08, SCENE.droneArm)
  }
  return b.build(false)
}

/** half diagonal of the model bounding box (R_vis, M06 §6.9); 0.6 m when unknown */
export function visRadius(g: BufferGeometry | null): number {
  if (!g) return 0.6
  g.computeBoundingBox()
  const bb = g.boundingBox
  if (!bb) return 0.6
  const dx = bb.max.x - bb.min.x, dy = bb.max.y - bb.min.y, dz = bb.max.z - bb.min.z
  return Math.max(0.1, 0.5 * hypot3(dx, dy, dz))
}

export function triangleCount(g: BufferGeometry): number {
  return (g.getAttribute('position').count / 3) | 0
}
