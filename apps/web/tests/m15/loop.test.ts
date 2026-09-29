// Frame phase scheduler skeleton (M06 §6.6; AWR-03 §3.6): phase order, fps quantisation, tier filters, unregister.
import { describe, expect, it } from 'vitest'
import { ctx, register, runPhase, type FrameCtx } from '@/engine/loop'

const frame = (nowMs: number, tier: FrameCtx['tier'] = 'A'): FrameCtx => ({ ...ctx, nowMs, tier })

describe('loop.register', () => {
  it('runs tasks of a phase by order and allows re-registration by id', () => {
    const seen: string[] = []
    const off1 = register('overlay', 't-b', () => seen.push('b'), { order: 2 })
    const off2 = register('overlay', 't-a', () => seen.push('a'), { order: 1 })
    register('overlay', 't-b', () => seen.push('b2'), { order: 0 })
    runPhase('overlay', frame(0))
    expect(seen).toEqual(['b2', 'a'])
    off1()
    off2()
  })

  it('quantises fps tasks to frame boundaries', () => {
    let n = 0
    const off = register('governor', 't-fps', () => n++, { fps: 4 })
    for (let t = 0; t <= 1000; t += 1000 / 60) runPhase('governor', frame(t))
    expect(n).toBeGreaterThanOrEqual(4)
    expect(n).toBeLessThanOrEqual(5)
    off()
  })

  it('skips tasks outside their tiers and survives a throwing task', () => {
    const seen: string[] = []
    const offs = [
      register('world', 't-ab', () => seen.push('ab'), { tiers: ['A', 'B'] }),
      register('world', 't-throw', () => { throw new Error('boom') }),
      register('world', 't-all', () => seen.push('all')),
    ]
    const err = console.error
    console.error = () => {}
    try {
      runPhase('world', frame(0, 'S'))
      runPhase('world', frame(1, 'A'))
    } finally {
      console.error = err
    }
    expect(seen).toEqual(['all', 'ab', 'all'])
    for (const off of offs) off()
  })
})
