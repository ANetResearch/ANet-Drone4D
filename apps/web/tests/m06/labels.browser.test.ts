// M06-AC-047 DOM part (M06 §6.13; ADR-029): the LabelLayer pool (Tier S 16), transform writes only when a label moved
// >= 0.5 px, display writes only on state changes, text batches at <= 4 Hz on Tier S, anchored above the point.
import { describe, expect, it } from 'vitest'
import { ctx as frameCtx, LABEL_PRIO, LabelKind, LabelLayer } from '@/engine'

describe('LabelLayer DOM writes (M06-AC-047)', () => {
  it('LabelLayer: pool, transform writes only on >= 0.5 px moves, text at 4 Hz on Tier S', () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    let x = 100
    const L = new LabelLayer('S', (_enu, out) => {
      out[0] = x
      out[1] = 200
      return true
    })
    L.mount(host)
    expect(L.poolSize).toBe(16)
    let texts = 0
    L.setTextFn((_k, key, _e, out) => {
      texts++
      out.id = `uav${key}`
      out.sub = 'FLYING'
    })
    const ctx = { ...frameCtx, tier: 'S' as const, cssW: 1280, cssH: 720 }
    const run = (now: number): typeof L.writes => {
      L.cand.clear()
      L.cand.add(LabelKind.Drone, 7, 0, 0, 0, LABEL_PRIO.selected, 0, false, false)
      L.layout({ ...ctx, nowMs: now })
      L.write({ ...ctx, nowMs: now })
      return { ...L.writes }
    }
    expect(run(0)).toMatchObject({ transform: 1, display: 1 })
    x += 0.3
    expect(run(33).transform).toBe(0)
    x += 0.4
    expect(run(66).transform).toBe(1)
    const t0 = texts
    for (let f = 3; f < 33; f++) run(f * 33.3)
    expect(texts - t0).toBeLessThanOrEqual(5) // ~1 s at 30 fps: <= 4 Hz text updates
    expect(L.shown()).toEqual([{ id: 'uav7', sub: 'FLYING', x: Math.round(x), y: 188 }])
    L.unmount()
  })
})
