// D2 WP-11 vehicle shapes (AWR-20 §4.1, §4.2, §7.1; M06-FR-034): every shape (hex D1 P600, quad, fixed wing, VTOL) stays
// within the low-poly budgets (Tier S <= 150 triangles, Tier B/A <= 300) with the same attribute set {position, normal,
// color} (one program per batch, M06 §6.4 rule 1); its bounding box matches the catalog size (fixed wing 1.4 x 2.6 m,
// VTOL 1.6 x 2.8 m, quad 0.6 m wheelbase class); the roster models map to shapes, instance scales and R_vis (D1 bodies
// keep 0.6 m), clones keep their source's shape, unknown models fall back by name, M21 registrations override; the
// drone layer draws a fixed wing in the fixed-wing batch at its instance scale and buckets it with its own R_vis.
import { describe, expect, it } from 'vitest'
import { Box3, Matrix4, PerspectiveCamera, Vector3 } from 'three'
import {
  ctx as frameCtx, DroneLayer, InterpRing, SHAPE_REF, SHAPES, makeShapeGeometry, modelKey, registerVehicleModel, shapeOf, triangleCount, vehicleShapesVersion,
  type RenderBackendView,
} from '@/engine'
import { GLPointsNodeMaterial } from '@/viewport/glPointsNodeMaterial'

const be = { tier: 'S', deviceClass: 'software', kind: 'webgl2', pointSizeMode: 'glpoint', startRung: 0, lowestAllowedRung: 0, caps: {},
  createPointsMaterial: () => new GLPointsNodeMaterial(), programsCount: () => 0 } as unknown as RenderBackendView

describe('low-poly shapes (AWR-20 §7.1)', () => {
  it('budgets, attribute set and catalog sizes', () => {
    for (const shape of SHAPES) {
      const s = makeShapeGeometry(shape, 's')
      const f = makeShapeGeometry(shape, 'full')
      expect(triangleCount(s), `${shape} s`).toBeLessThanOrEqual(150)
      expect(triangleCount(f), `${shape} full`).toBeLessThanOrEqual(300)
      expect(triangleCount(f)).toBeGreaterThanOrEqual(triangleCount(s))
      for (const g of [s, f]) expect(Object.keys(g.attributes).sort()).toEqual(['color', 'normal', 'position'])
      const bb = new Box3().setFromBufferAttribute(f.getAttribute('position') as never)
      const size = bb.getSize(new Vector3())
      // x = length (FLU forward), y = span; within 12 % of the reference box (the quad reference is its 0.6 m motor
      // diagonal: the drawn box adds the 0.186 m rotor discs around motors at 45 deg)
      const q = 2 * (0.3 * Math.SQRT1_2 + 0.186)
      const ref = shape === 'quad' ? [q, q, 0.16] : SHAPE_REF[shape]
      expect(Math.abs(size.x - ref[0]) / ref[0], `${shape} length ${size.x}`).toBeLessThan(0.12)
      expect(Math.abs(size.y - ref[1]) / ref[1], `${shape} span ${size.y}`).toBeLessThan(0.12)
    }
  })
  it('roster models to shapes, scales and R_vis; clones, fallbacks and registrations', () => {
    expect(modelKey('awr_f1_c2')).toBe('awr-f1')
    expect(modelKey('p600_mid360')).toBe('p600')
    const p = shapeOf('p600')
    expect([p.shape, p.scale, p.rVis]).toEqual(['hex', 1, 0.6])
    expect(shapeOf('x500').shape).toBe('quad')
    expect(shapeOf('x500').rVis).toBe(0.6)
    expect(shapeOf('awr-q1').shape).toBe('quad')
    expect(shapeOf('awr-q1').scale).toBeCloseTo(0.38 / 0.6, 9)
    expect(shapeOf('awr-q9').scale).toBeCloseTo(0.9 / 0.6, 9)
    expect(shapeOf('awr-f1').shape).toBe('fw')
    expect(shapeOf('awr-f1').scale).toBeCloseTo(1, 9)
    expect(shapeOf('awr-f1').rVis).toBeCloseTo(0.5 * Math.hypot(1.4, 2.6, 0.35), 9)
    expect(shapeOf('awr-v1').shape).toBe('vtol')
    expect(shapeOf('awr_v1_c1').shape).toBe('vtol')
    expect(shapeOf('awr-f7').shape).toBe('fw')
    expect(shapeOf('foo').shape).toBe('hex')
    const v0 = vehicleShapesVersion()
    registerVehicleModel('awr-x2', { frame: 'fixed_wing', sizeM: [2.8, 5.2, 0.7] })
    expect(vehicleShapesVersion()).toBe(v0 + 1)
    expect(shapeOf('awr-x2').shape).toBe('fw')
    expect(shapeOf('awr-x2').scale).toBeCloseTo(2, 9)
  })
  it('the drone layer draws a fixed wing in the fw batch at its scale, with its own R_vis', () => {
    const interp = new InterpRing()
    const ids = new Map([[1, { id: 'f1-01', model: 'awr-f1', kind: 'uav' }], [2, { id: 'p600-01', model: 'p600', kind: 'uav' }]])
    for (const a of [1, 2]) {
      const s = interp.slotFor(a)
      interp.push(s, 0, a * 20, 0, 50, 0, 0, 0, 0, 0, 0, 1, 0)
      interp.push(s, 100, a * 20, 0, 50, 0, 0, 0, 0, 0, 0, 1, 0)
    }
    interp.eMaxMs = 1e9
    const roster = { get size() { return ids.size }, version: 1, get: (a: number) => ids.get(a), idOf: (a: number) => ids.get(a)?.id }
    const layer = new DroneLayer({ be, interp, roster: () => roster, subscribe: () => () => {} })
    const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
    cam.position.set(30, 60, 40)
    cam.lookAt(30, 50, 0)
    cam.updateMatrixWorld()
    for (let k = 0; k < 3; k++) layer.update({ ...frameCtx, nowMs: 50 + 200 * k, tRenderS: 0.05, tFocusS: 0.05, camera: cam, cssW: 1280, cssH: 720, dbW: 640, dbH: 360, dpr: 0.5 })
    expect(layer.rVisOf[1]).toBeCloseTo(shapeOf('awr-f1').rVis, 6)
    expect(layer.rVisOf[2]).toBeCloseTo(0.6, 6)
    const fw = layer.lowBatches().find((b) => b.mesh.name === 'DroneLowPoly.fw')!
    const hex = layer.lowBatches().find((b) => b.mesh.name === 'DroneLowPoly.hex')!
    expect(fw.count).toBe(1)
    const m = new Matrix4()
    fw.mesh.getMatrixAt(0, m)
    const sc = new Vector3().setFromMatrixScale(m)
    expect(sc.x).toBeCloseTo(shapeOf('awr-f1').scale, 6)
    expect(hex.count + layer.hero.count).toBe(1)
    expect(layer.lowBatches().map((b) => b.mesh.name)).toEqual(['DroneLowPoly.hex', 'DroneLowPoly.quad', 'DroneLowPoly.fw', 'DroneLowPoly.vtol'])
    layer.dispose()
  })
})
