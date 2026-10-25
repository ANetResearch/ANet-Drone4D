import { expect, it } from 'vitest'
import { rehearsalFrame } from '@/ui/panels/targets/rehearsalFrame'
import type { LifeSample } from '@/ui/sandbox/lifeApi'

const first: LifeSample = { t: 0, pos: [0, 0, 0], yaw: Math.PI - .1, speed: 2, distance: 0, hidden: false, phase: 'travel', destination: 'Shop', reason: '' }
const second: LifeSample = { ...first, t: 1, pos: [2, 0, 0], yaw: -Math.PI + .1, distance: 2 }
it('interpolates position and gait distance with the shortest heading turn', () => {
  const mid = rehearsalFrame([first, second], .5)
  expect(mid.pos).toEqual([1, 0, 0])
  expect(mid.distance).toBe(1)
  expect(mid.yaw).toBeCloseTo(Math.PI)
  expect(first.pos).toEqual([0, 0, 0])
})
it('keeps facility entry discrete and clamps endpoint seeks', () => {
  const inside = { ...second, hidden: true, phase: 'visit' }
  expect(rehearsalFrame([first, inside], .9)).toEqual(first)
  expect(rehearsalFrame([first, inside], 1)).toEqual(inside)
  expect(rehearsalFrame([first, inside], 999)).toEqual(inside)
  expect(rehearsalFrame([first, inside], -10)).toEqual(first)
})
