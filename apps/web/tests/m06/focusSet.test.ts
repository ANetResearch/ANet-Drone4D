// M06-AC-029 (ADR-046; M06 §6.9): focus set K <= 32, 8 px in / 6 px out with 1 s dwell, 250 ms cadence driven by the
// caller, incremental 30 Hz subscriptions through the injected RtClient (reference counted there), selected vehicles
// excluded, vanished vehicles released at once; subscription changes for 1000 vehicles stay a few per update.
import { describe, expect, it } from 'vitest'
import { FOCUS, FocusSet } from '@/engine'

function harness() {
  const subs = new Map<string, number>()
  let calls = 0
  const fs = new FocusSet({
    idOf: (a) => `uav${a}`,
    subscribe: (topic, rate) => {
      calls++
      expect(rate).toBe(FOCUS.rate)
      subs.set(topic, (subs.get(topic) ?? 0) + 1)
      return () => {
        calls++
        subs.set(topic, (subs.get(topic) ?? 1) - 1)
        if (subs.get(topic) === 0) subs.delete(topic)
      }
    },
  })
  return { fs, subs, calls: () => calls }
}

describe('focus set (M06-AC-029)', () => {
  it('K <= 32 by descending r_px, selected excluded', () => {
    const h = harness()
    const n = 100
    const agent = new Uint16Array(n).map((_, i) => i)
    const rpx = new Float32Array(n).map((_, i) => 8 + i)
    const excluded = new Uint8Array(65536)
    excluded[99] = 1
    h.fs.update(n, agent, rpx, 0, excluded)
    expect(h.fs.size).toBe(32)
    expect(h.subs.size).toBe(32)
    expect(h.fs.has(98)).toBe(true)
    expect(h.fs.has(99)).toBe(false)
    expect(h.fs.has(0)).toBe(false)
  })

  it('8 px to enter, < 6 px to leave only after 1 s; vanished vehicles leave at once', () => {
    const h = harness()
    const agent = new Uint16Array([1, 2])
    const ex = new Uint8Array(65536)
    h.fs.update(2, agent, new Float32Array([7.9, 8.1]), 0, ex)
    expect([h.fs.has(1), h.fs.has(2)]).toEqual([false, true])
    h.fs.update(2, agent, new Float32Array([7.9, 6.5]), 250, ex) // between 6 and 8: stays
    expect(h.fs.has(2)).toBe(true)
    h.fs.update(2, agent, new Float32Array([7.9, 5.0]), 500, ex) // below 6 but dwell < 1 s
    expect(h.fs.has(2)).toBe(true)
    h.fs.update(2, agent, new Float32Array([7.9, 5.0]), 1000, ex) // dwell reached
    expect(h.fs.has(2)).toBe(false)
    h.fs.update(1, new Uint16Array([2]), new Float32Array([20]), 1250, ex)
    expect(h.fs.has(2)).toBe(true)
    h.fs.update(0, new Uint16Array(0), new Float32Array(0), 1500, ex) // vanished
    expect(h.fs.size).toBe(0)
    expect(h.subs.size).toBe(0)
  })

  it('1000 vehicles moving: incremental changes only (<= a few subscription calls per 250 ms update)', () => {
    const h = harness()
    const n = 1000
    const agent = new Uint16Array(n).map((_, i) => i)
    const ex = new Uint8Array(65536)
    let s = 7
    const rnd = (): number => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296
    const rpx = new Float32Array(n).map(() => rnd() * 12)
    h.fs.update(n, agent, rpx, 0, ex)
    const base = h.calls()
    let maxPerUpdate = 0
    for (let k = 1; k <= 40; k++) {
      for (let i = 0; i < n; i++) rpx[i] = Math.max(0, rpx[i] + (rnd() - 0.5) * 0.2)
      const c0 = h.calls()
      h.fs.update(n, agent, rpx, k * 250, ex)
      maxPerUpdate = Math.max(maxPerUpdate, h.calls() - c0)
      expect(h.fs.size).toBeLessThanOrEqual(32)
    }
    expect(base).toBe(32)
    expect(maxPerUpdate).toBeLessThanOrEqual(8)
  })
})
