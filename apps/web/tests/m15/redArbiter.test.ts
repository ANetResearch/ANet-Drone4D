// M15-FR-091 (AWR-15 §3.7.2): one red per figure; critical > selected > hero; dwell 1.5 s against same-level,
// same-or-lower-rank candidates; a higher level pre-empts at once; selected switches at once.
import { describe, expect, it } from 'vitest'
import { arbitrate, RED_NONE, sameEntity, type RedCandidate, type RedState } from '@/lib/redArbiter'
import { INPUT } from '@/lib/tokens/input.gen'

const drone = (id: string) => ({ kind: 'drone' as const, id })
const c = (id: string, level: RedCandidate['level'], rank = 0, t = 0): RedCandidate => ({ entity: drone(id), level, rank, tLastWallMs: t })

describe('arbitrate', () => {
  it('keeps RED_NONE without candidates', () => {
    expect(arbitrate(RED_NONE, [], 0)).toBe(RED_NONE)
  })

  it('orders critical over selected over hero', () => {
    const s = arbitrate(RED_NONE, [c('h', 'hero'), c('s', 'selected'), c('x', 'critical', 1)], 100)
    expect(s.owner).toEqual(drone('x'))
    expect(s.level).toBe('critical')
    expect(arbitrate(RED_NONE, [c('h', 'hero'), c('s', 'selected')], 0).owner).toEqual(drone('s'))
  })

  it('picks the higher rank, then the latest trigger among criticals', () => {
    expect(arbitrate(RED_NONE, [c('a', 'critical', 1, 5), c('b', 'critical', 2, 1)], 0).owner?.id).toBe('b')
    expect(arbitrate(RED_NONE, [c('a', 'critical', 1, 5), c('b', 'critical', 1, 9)], 0).owner?.id).toBe('b')
  })

  it('dwells a critical owner against a newer same-rank critical', () => {
    const s0 = arbitrate(RED_NONE, [c('a', 'critical', 1, 0)], 0)
    const cands = [c('a', 'critical', 1, 0), c('b', 'critical', 1, 10)]
    expect(arbitrate(s0, cands, INPUT.redDwellMs - 1)).toBe(s0)
    const s2 = arbitrate(s0, cands, INPUT.redDwellMs)
    expect(s2.owner?.id).toBe('b')
    expect(s2.ownerSinceMs).toBe(INPUT.redDwellMs)
  })

  it('lets a higher rank or a higher level pre-empt during the dwell', () => {
    const s0 = arbitrate(RED_NONE, [c('a', 'critical', 1)], 0)
    expect(arbitrate(s0, [c('a', 'critical', 1), c('b', 'critical', 3)], 10).owner?.id).toBe('b')
    const h0 = arbitrate(RED_NONE, [c('h', 'hero')], 0)
    expect(arbitrate(h0, [c('h', 'hero'), c('s', 'selected')], 10).owner?.id).toBe('s')
  })

  it('switches selected owners at once', () => {
    const s0 = arbitrate(RED_NONE, [c('a', 'selected', 0, 0)], 0)
    expect(arbitrate(s0, [c('b', 'selected', 0, 5)], 1).owner?.id).toBe('b')
  })

  it('clears the owner when every candidate is gone and keeps identity when unchanged', () => {
    const s0: RedState = arbitrate(RED_NONE, [c('a', 'hero')], 0)
    expect(arbitrate(s0, [c('a', 'hero')], 99_999)).toBe(s0)
    const cleared = arbitrate(s0, [], 50)
    expect(cleared.owner).toBeNull()
    expect(cleared.ownerSinceMs).toBe(50)
  })

  it('compares entities by kind and id', () => {
    expect(sameEntity(drone('1'), drone('1'))).toBe(true)
    expect(sameEntity(drone('1'), { kind: 'zone', id: '1' })).toBe(false)
    expect(sameEntity(null, drone('1'))).toBe(false)
  })
})
