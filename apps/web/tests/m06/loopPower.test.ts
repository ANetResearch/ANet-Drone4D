// D2.1 WP-11b (AWR-04 §15.7.7; ADR-121; D2-AC-44 Node part): suspension stops the rAF chain (no callback per display frame),
// requestFrame() renders exactly one frozen frame while suspended, wake(ms) keeps frames running until ms after the last
// call, resuming restarts with dt 0 and 30 frozen frames; the quality frame rate limits frames without freezing (the phone's
// 30 fps keeps CAS and PerfGovernor working), unlike a frame cap.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ctx, frame, loop, register } from '@/engine/loop'

type Cb = (t: number) => void
let queue: Map<number, Cb>
let nextId = 1
let now = 0

function flush(stepMs = 16.7): number {
  // one display frame: run the callbacks queued before it
  now += stepMs
  const cbs = [...queue.entries()]
  queue.clear()
  for (const [, cb] of cbs) cb(now)
  return cbs.length
}

const g = globalThis as unknown as { requestAnimationFrame?: (cb: Cb) => number; cancelAnimationFrame?: (id: number) => void; performance: Performance }
let realNow: () => number

beforeEach(() => {
  queue = new Map()
  g.requestAnimationFrame = (cb) => {
    const id = nextId++
    queue.set(id, cb)
    return id
  }
  g.cancelAnimationFrame = (id) => {
    queue.delete(id)
  }
  realNow = performance.now.bind(performance)
  performance.now = () => now
})
afterEach(() => {
  loop.stop()
  loop.setSuspended(false)
  loop.setQualityFps(0)
  loop.setFrameCap(0)
  queue.clear()
  delete g.requestAnimationFrame
  delete g.cancelAnimationFrame
  performance.now = realNow
})

describe('loop power save (AWR-04 §15.7.7)', () => {
  it('suspended: no rAF callbacks; requestFrame renders one frozen frame; resume restarts with dt 0', () => {
    let frames = 0
    let frozen: boolean[] = []
    const off = register('world', 't-power', (c) => {
      frames++
      frozen.push(c.frozen)
    })
    loop.start()
    flush()
    flush()
    expect(frames).toBe(2)
    loop.setSuspended(true)
    expect(queue.size).toBe(0)
    for (let i = 0; i < 30; i++) expect(flush()).toBe(0)
    expect(frames).toBe(2)
    const s0 = loop.suspendedFrames
    frozen = []
    loop.requestFrame()
    loop.requestFrame() // coalesced
    flush()
    expect(frames).toBe(3)
    expect(frozen).toEqual([true])
    expect(loop.suspendedFrames).toBe(s0 + 1)
    expect(flush()).toBe(0)
    loop.setSuspended(false)
    frozen = []
    flush()
    expect(frames).toBe(4)
    expect(ctx.dtMs).toBe(0)
    expect(frozen).toEqual([true]) // 30 frozen frames after the gap
    off()
  })

  it('wake(ms): frames while suspended until ms after the call', () => {
    let frames = 0
    const off = register('world', 't-wake', () => frames++)
    loop.start()
    loop.setSuspended(true)
    loop.wake(100)
    for (let i = 0; i < 10; i++) flush(16.7)
    // about 100 / 16.7 = 6 frames
    expect(frames).toBeGreaterThanOrEqual(5)
    expect(frames).toBeLessThanOrEqual(7)
    const f = frames
    for (let i = 0; i < 10; i++) flush(16.7)
    expect(frames).toBe(f)
    off()
  })

  it('the quality frame rate skips frames without freezing; a frame cap freezes', () => {
    let t = 5000
    while (loop.freezeFrames > 0) frame((t += 16.7)) // the resume of the previous test froze 30 frames
    const frozen: boolean[] = []
    const off = register('world', 't-q', (c) => frozen.push(c.frozen))
    loop.setQualityFps(30)
    for (let i = 1; i <= 60; i++) frame(10_000 + i * 16.7)
    expect(frozen.length).toBeGreaterThanOrEqual(29)
    expect(frozen.length).toBeLessThanOrEqual(31)
    expect(frozen.every((x) => !x)).toBe(true)
    loop.setQualityFps(0)
    frozen.length = 0
    loop.setFrameCap(15)
    for (let i = 1; i <= 60; i++) frame(20_000 + i * 16.7)
    expect(frozen.every((x) => x)).toBe(true)
    off()
  })
})
