// M06-AC-015 (AWR-03 §3.6): phase order with the R3F advance hook in seconds, the render phase inside advance, fps
// tasks at 3.5-4.0 Hz on a 30 fps cadence, freeze frames, events, our-logic timing excluding the render phase.
import { afterEach, describe, expect, it } from 'vitest'
import { ctx, events, frame, frameTiming, loop, register, phaseMs, PHASES } from '@/engine/loop'

const offs: (() => void)[] = []
afterEach(() => {
  while (offs.length) offs.pop()!()
  loop.setAdvance(null)
  loop.setFrameCap(0)
})

describe('engine/loop frame order (M06-AC-015)', () => {
  it('runs telemetry..world, advance(seconds) with render inside, then overlay and governor', () => {
    const seen: string[] = []
    for (const p of PHASES) offs.push(register(p, `t-${p}`, () => seen.push(p)))
    let advT = -1
    loop.setAdvance((t) => {
      advT = t
      seen.push('advance')
      loop.runPhase('render', ctx)
    })
    frame(1000)
    expect(seen).toEqual(['telemetry', 'clock', 'drones', 'camera', 'world', 'advance', 'render', 'overlay', 'governor'])
    expect(advT).toBe(1) // seconds (R3F 9.8.1 frameloop="never")
  })

  it('fps: 4 runs 3.5-4.0 times per second at 30 fps and is quantised to frame boundaries', () => {
    let n = 0
    offs.push(register('governor', 't-fps4', () => n++, { fps: 4 }))
    const t0 = 50_000
    for (let k = 0; k < 30 * 10; k++) frame(t0 + (k * 1000) / 30)
    const hz = n / 10
    expect(hz).toBeGreaterThanOrEqual(3.5)
    expect(hz).toBeLessThanOrEqual(4.05)
  })

  it('tier filters, freeze frames and frame-cap freeze', () => {
    let sOnly = 0
    offs.push(register('world', 't-s', () => sOnly++, { tiers: ['S'] }))
    loop.setTier('B', 'dGPU')
    frame(200_000)
    expect(sOnly).toBe(0)
    loop.setTier('S', 'software')
    frame(200_033)
    expect(sOnly).toBe(1)
    loop.freeze(2)
    frame(200_066)
    expect(ctx.frozen).toBe(true)
    frame(200_100)
    expect(ctx.frozen).toBe(true)
    frame(200_133)
    expect(ctx.frozen).toBe(false)
    loop.setFrameCap(15)
    frame(200_200)
    expect(ctx.frozen).toBe(true)
  })

  it('our logic excludes the render phase; phase times are recorded', () => {
    const spin = (ms: number): void => {
      const e = performance.now() + ms
      while (performance.now() < e) { /* busy */ }
    }
    offs.push(register('render', 't-busy-render', () => spin(8)))
    offs.push(register('overlay', 't-busy-overlay', () => spin(4)))
    frame(300_000)
    expect(frameTiming.renderMs).toBeGreaterThanOrEqual(7.5)
    expect(phaseMs[PHASES.indexOf('overlay')]).toBeGreaterThanOrEqual(3.5)
    expect(frameTiming.oursMs).toBeGreaterThanOrEqual(3.5)
    expect(frameTiming.oursMs).toBeLessThan(frameTiming.frameMs - 7)
  })

  it('synchronous events', () => {
    const got: unknown[] = []
    const off = events.on('camera.mode', (m) => got.push(m))
    events.emit('camera.mode', { mode: 'bird' })
    off()
    events.emit('camera.mode', { mode: 'orbit' })
    expect(got).toEqual([{ mode: 'bird' }])
  })
})
