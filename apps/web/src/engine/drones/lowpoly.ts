// Procedural low-poly vehicles (M06-FR-034; AWR-15 §10.4; ADR-022; D2 WP-11: AWR-20 §7.1 shapes). Owner: M06.
// Built in FLU (x forward, y left, z up) and centred on the body. Vertex colours carry the part (body --drone-body, rear
// arms and tail --drone-arm, front arms and the nose --drone-arm-front, rotor rings and prop discs --drone-rotor; the
// brightest parts show the nose by lightness, not hue). 's' (Tier S, lowpoly_s) stays <= 150 triangles, 'full' (Tier B/A,
// lowpoly) <= 300. Flat shading: non-indexed with face normals. Every shape has the same attribute set {position, normal,
// color}, so the low-poly batches differ only by geometry (M06 §6.4 rule 1). Shapes (engine/drones/vehicleShapes.ts):
//   hex   the D1 P600 hexacopter (unchanged);
//   quad  X-configuration quadcopter at a 0.6 m wheelbase (X500, AWR-Q1, AWR-Q9 by instance scale);
//   fw    AWR-F1 pusher fixed wing (1.4 m x 2.6 m): fuselage with nose, tapered main wing, twin tail booms, H-tail with two
//         fins, pusher prop disc behind the fuselage (AWR-20 §7.1);
//   vtol  AWR-V1 quadplane (1.6 m x 2.8 m): the fixed wing plus two lift booms and four lift rotors on a 1.25 m diagonal.
// The hero geometry (placeholder for p600.glb) has {position, normal} only.
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
  /** flat annulus in the y-z plane (a prop disc facing along x) centred at (x, y, z) */
  ringX(x: number, y: number, z: number, r: number, w: number, n: number, rgb: readonly number[]): void {
    for (let i = 0; i < n; i++) {
      const a0 = (2 * Math.PI * i) / n, a1 = (2 * Math.PI * (i + 1)) / n
      const o0: V3 = [x, y + r * Math.cos(a0), z + r * Math.sin(a0)], o1: V3 = [x, y + r * Math.cos(a1), z + r * Math.sin(a1)]
      const i0: V3 = [x, y + (r - w) * Math.cos(a0), z + (r - w) * Math.sin(a0)], i1: V3 = [x, y + (r - w) * Math.cos(a1), z + (r - w) * Math.sin(a1)]
      this.quad(i0, i1, o1, o0, rgb)
    }
  }
  /** tapered flat panel (wing, stabiliser) along y: root chord at y = 0, tip chord at y = +-span/2, leading edge at x0, thickness t */
  wing(x0: number, z: number, span: number, rootChord: number, tipChord: number, t: number, sweep: number, rgb: readonly number[]): void {
    const h = span / 2
    for (const side of [1, -1]) {
      const yr = 0, yt = side * h
      const lr: V3 = [x0, yr, z], tr: V3 = [x0 - rootChord, yr, z]
      const lt: V3 = [x0 - sweep, yt, z], tt: V3 = [x0 - sweep - tipChord, yt, z]
      const up = (p: V3): V3 => [p[0], p[1], p[2] + t / 2]
      const dn = (p: V3): V3 => [p[0], p[1], p[2] - t / 2]
      // top and bottom faces (winding by side so the normals point up and down), leading and trailing edges, tip
      if (side > 0) {
        this.quad(up(lr), up(lt), up(tt), up(tr), rgb)
        this.quad(dn(tr), dn(tt), dn(lt), dn(lr), rgb)
        this.quad(dn(lr), dn(lt), up(lt), up(lr), rgb)
        this.quad(up(tr), up(tt), dn(tt), dn(tr), rgb)
        this.quad(dn(lt), dn(tt), up(tt), up(lt), rgb)
      } else {
        this.quad(up(lr), up(tr), up(tt), up(lt), rgb)
        this.quad(dn(tr), dn(lr), dn(lt), dn(tt), rgb)
        this.quad(dn(lr), up(lr), up(lt), dn(lt), rgb)
        this.quad(up(tr), dn(tr), dn(tt), up(tt), rgb)
        this.quad(dn(lt), up(lt), up(tt), dn(tt), rgb)
      }
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

/** X-configuration quadcopter at the 0.6 m reference wheelbase (vehicleShapes.ts SHAPE_REF.quad); 's' <= 150, 'full' <= 300 */
export function makeQuadGeometry(detail: 's' | 'full' = 's'): BufferGeometry {
  const b = new Builder()
  const arm = 0.3 // half the wheelbase (motor to centre)
  const bodyW = 0.16, bodyH = 0.07
  b.box(0, 0, 0, bodyW * 1.3, bodyW, bodyH, SCENE.droneBody)
  for (const deg of [45, 135, 225, 315]) {
    const a = (deg * Math.PI) / 180
    const front = Math.cos(a) > 0
    b.prism(a, bodyW * 0.45, arm, 0.035, bodyH * 0.3, front ? SCENE.droneArmFront : SCENE.droneArm)
    b.ring(Math.cos(a) * arm, Math.sin(a) * arm, bodyH * 0.5, 0.186, 0.02, detail === 's' ? 8 : 16, SCENE.droneRotor)
    if (detail === 'full') b.box(Math.cos(a) * arm, Math.sin(a) * arm, bodyH * 0.25, 0.04, 0.04, 0.06, SCENE.droneArm)
  }
  if (detail === 'full') b.box(0.02, 0, bodyH * 0.8, bodyW * 0.7, bodyW * 0.55, bodyH * 0.6, SCENE.droneBody)
  return b.build(true)
}

/** fuselage, nose, wing, twin booms, H-tail and pusher disc of a fixed wing of length l and span s (body centred) */
function fixedWing(b: Builder, l: number, s: number, h: number, detail: 's' | 'full', boomY: number): void {
  const xNose = 0.5 * l, xTail = -0.5 * l
  const fusW = 0.11 * s * 0.5, fusH = 0.5 * h
  const xFusEnd = xTail + 0.36 * l
  // fuselage box from the end (prop) to behind the nose, then a 4-face nose cone
  const xFusFront = xNose - 0.12 * l
  b.box((xFusFront + xFusEnd) / 2, 0, 0, xFusFront - xFusEnd, fusW, fusH, SCENE.droneBody)
  const n: V3 = [xNose, 0, -0.05 * fusH]
  const c0: V3 = [xFusFront, fusW / 2, fusH / 2], c1: V3 = [xFusFront, -fusW / 2, fusH / 2], c2: V3 = [xFusFront, -fusW / 2, -fusH / 2], c3: V3 = [xFusFront, fusW / 2, -fusH / 2]
  b.tri(c0, c1, n, SCENE.droneArmFront)
  b.tri(c1, c2, n, SCENE.droneArmFront)
  b.tri(c2, c3, n, SCENE.droneArmFront)
  b.tri(c3, c0, n, SCENE.droneArmFront)
  // main wing: leading edge at 40 % of the length from the nose, taper 0.6, slight sweep
  const root = 0.21 * l, tip = 0.6 * root
  b.wing(xNose - 0.36 * l, 0.25 * fusH, s, root, tip, 0.04 * h + 0.012, 0.03 * l, SCENE.droneBody)
  // twin tail booms from the wing to the tail, H-tail between them, two fins
  const boomW = 0.025 * l
  const xb0 = xNose - 0.4 * l
  for (const sy of [1, -1]) b.box((xb0 + xTail) / 2, sy * boomY, 0.25 * fusH, xb0 - xTail, boomW, boomW, SCENE.droneArm)
  const stab = 0.12 * l
  b.wing(xTail + stab, 0.25 * fusH + 0.5 * boomW, 2 * boomY + 2 * boomW, stab, stab, 0.012, 0, SCENE.droneArm)
  const finH = 0.6 * h
  for (const sy of [1, -1]) {
    const y = sy * boomY
    const a: V3 = [xTail + stab, y, 0.25 * fusH + 0.5 * boomW], d: V3 = [xTail, y, 0.25 * fusH + 0.5 * boomW], t: V3 = [xTail + 0.15 * stab, y, 0.25 * fusH + finH]
    b.tri(a, d, t, SCENE.droneArm)
    b.tri(d, a, t, SCENE.droneArm)
  }
  // pusher prop disc right behind the fuselage, facing aft
  b.ringX(xFusEnd - 0.02 * l, 0, 0, 0.12 * l, 0.03 * l, detail === 's' ? 6 : 16, SCENE.droneRotor)
}

/** AWR-F1 pusher fixed wing at its catalog size (vehicleShapes.ts SHAPE_REF.fw: 1.4 m x 2.6 m x 0.35 m) */
export function makeFixedWingGeometry(detail: 's' | 'full' = 's'): BufferGeometry {
  const b = new Builder()
  fixedWing(b, 1.4, 2.6, 0.35, detail, 0.22)
  if (detail === 'full') b.box(0.15, 0, -0.12, 0.25, 0.12, 0.06, SCENE.droneArm) // EO / LWIR pod under the nose
  return b.build(true)
}

/** AWR-V1 quadplane at its catalog size (SHAPE_REF.vtol: 1.6 m x 2.8 m x 0.45 m): fixed wing + lift booms + 4 lift rotors */
export function makeVtolGeometry(detail: 's' | 'full' = 's'): BufferGeometry {
  const b = new Builder()
  const arm = 0.4419 // AWR-20 §4.2 geometry.arm_xy_m (1.25 m diagonal)
  fixedWing(b, 1.6, 2.8, 0.45, detail, arm)
  // the lift booms carry the tail (fixedWing put the twin booms at y = +-arm); extend them forward past the front rotors
  const z = 0.15
  for (const sy of [1, -1]) {
    // the booms run forward past the front rotors (Tier B/A detail; Tier S keeps the 150-triangle budget)
    if (detail === 'full') b.box(arm + 0.05, sy * arm, 0.06, 0.2, 0.04, 0.04, SCENE.droneArmFront)
    for (const sx of [1, -1]) b.ring(sx * arm, sy * arm, z, 0.229, 0.025, detail === 's' ? 6 : 12, SCENE.droneRotor)
  }
  if (detail === 'full') b.box(0.2, 0, -0.15, 0.3, 0.14, 0.08, SCENE.droneArm)
  return b.build(true)
}

/** low-poly geometry of a shape (vehicleShapes.ts) */
export function makeShapeGeometry(shape: 'hex' | 'quad' | 'fw' | 'vtol', detail: 's' | 'full'): BufferGeometry {
  return shape === 'quad' ? makeQuadGeometry(detail) : shape === 'fw' ? makeFixedWingGeometry(detail) : shape === 'vtol' ? makeVtolGeometry(detail) : makeLowPolyGeometry(detail)
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
