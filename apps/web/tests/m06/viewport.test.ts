// M06 D1-MS3 pieces (M06 §6.2, §6.5, §6.7; AWR-14 §6.7): device classification and start rungs, the pass plan sum of
// the layer registry, the GoTo altitude rule, ENU and three frame mapping (ADR-002), bezier easing tokens.
import { describe, expect, it } from 'vitest'
import { Vector3 } from 'three'
import { bezierAt, enuToThree, threeToEnu } from '@/engine'
import { EASE } from '@/lib/tokens/motion.gen'
import { classifyRenderer, dprFor, lowestRungFor, startRungFor } from '@/viewport/renderer'
import { plannedDraws, registerLayer } from '@/viewport/layers/registry'
import { gotoTargetFor, GOTO } from '@/viewport/gotoRule'
import { vp } from '@/viewport/session'
import { ctx } from '@/engine/loop'

describe('RenderBackend classification (ADR-044; M06 §6.2.2)', () => {
  it('software renderers are Tier S software, iGPU families by name, the rest dGPU', () => {
    expect(classifyRenderer('ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)')).toEqual({ software: true, deviceClass: 'software' })
    expect(classifyRenderer('llvmpipe (LLVM 15.0.7, 256 bits)').software).toBe(true)
    expect(classifyRenderer('ANGLE (Intel, Intel(R) UHD Graphics 620, OpenGL 4.6)').deviceClass).toBe('iGPU')
    expect(classifyRenderer('ANGLE (NVIDIA, NVIDIA GeForce RTX 3080)').deviceClass).toBe('dGPU')
    expect([startRungFor('software'), startRungFor('iGPU'), startRungFor('dGPU')]).toEqual([0, 3, 4])
    expect([lowestRungFor('S'), lowestRungFor('B')]).toEqual([0, 2])
    expect([dprFor('software', 2), dprFor('iGPU', 2), dprFor('dGPU', 3)]).toEqual([0.5, 1.5, 2])
  })
})

describe('layer registry pass plan (AWR-03 §3.6 rule 2)', () => {
  it('sums drawCount of the registered layers', () => {
    const off1 = registerLayer({ id: 'debug', owner: 'M06', perfKey: 'mainJs', root: null, channel: 0, drawCount: () => 2, setVisible: () => {}, dispose: () => {} })
    const off2 = registerLayer({ id: 'zones', owner: 'M06', perfKey: 'trails', root: null, channel: 0, drawCount: () => 1, setVisible: () => {}, dispose: () => {} })
    expect(plannedDraws(ctx)).toBe(3)
    off1()
    off2()
    expect(plannedDraws(ctx)).toBe(0)
  })
})

describe('GoTo altitude rule (AWR-14 §6.7)', () => {
  it('keeps the current world z but never below hit z + 10 m', () => {
    const saved = vp.drones
    vp.drones = { poseOf: (no: number, out: Float64Array) => {
      if (no !== 3) return false
      out[0] = 1
      out[1] = 2
      out[2] = 40
      return true
    } } as unknown as typeof vp.drones
    expect(gotoTargetFor([5, 6, 12], 3)).toEqual([5, 6, 40])
    expect(gotoTargetFor([5, 6, 35], 3)).toEqual([5, 6, 35 + GOTO.minAboveHitM])
    expect(gotoTargetFor([5, 6, 12], -1)).toEqual([5, 6, 22])
    vp.drones = saved
  })
})

describe('frames and easing', () => {
  it('ENU (E, N, U) maps to three (E, U, -N) and back (ADR-002)', () => {
    const v = enuToThree(10, 20, 30, new Vector3())
    expect([v.x, v.y, v.z]).toEqual([10, 30, -20])
    expect(Array.from(threeToEnu(v, new Float64Array(3)))).toEqual([10, 20, 30])
    // WorldRoot rotation.x = -pi/2 produces the same mapping
    const r = new Vector3(10, 20, 30).applyAxisAngle(new Vector3(1, 0, 0), -Math.PI / 2)
    expect(r.x).toBeCloseTo(10, 9)
    expect(r.y).toBeCloseTo(30, 9)
    expect(r.z).toBeCloseTo(-20, 9)
  })
  it('bezier easing matches the smooth-out token ends and is monotonic', () => {
    expect(bezierAt(EASE.smoothOut, 0)).toBe(0)
    expect(bezierAt(EASE.smoothOut, 1)).toBe(1)
    let prev = 0
    for (let x = 0.05; x < 1; x += 0.05) {
      const y = bezierAt(EASE.smoothOut, x)
      expect(y).toBeGreaterThanOrEqual(prev)
      prev = y
    }
    expect(bezierAt(EASE.linear, 0.3)).toBeCloseTo(0.3, 5)
  })
})
