// M06-AC-052 pseudo-clock part (ADR-041; AWR-18 §4.7; M06 §6.17): under a CAS held at the quality floor the governor
// degrades 1 trails -> 2 frustums -> 3 labels -> 4 low-poly -> 5 environment -> 6 motion -> 7 floor release, sub-steps of
// a knob consecutively, >= 2 s apart, 7 last; with CAS at the ceiling for >= 10 s it restores in exactly the reverse
// order, >= 10 s apart, never degrading meanwhile; hardware tiers degrade layers only once the CAS outer loop reached the
// lowest allowed rung (2); frozen frames are not evaluated; history, events and the step label follow every change.
import { describe, expect, it } from 'vitest'
import { ctx as frameCtx, events, PerfGovernor, type CasHandle, type CasState, type FrameCtx, type GovernorKnob } from '@/engine'
import { makeProbe, type AwrPerf } from '@/engine/perf/probe'

function setup(tier: 'S' | 'B') {
  const st: CasState = { atFloorSinceMs: 0, atCeilSinceMs: 0, B: 20000, lo: 10000, hi: 40000, rung: tier === 'S' ? 0 : 4 }
  let floorOverride: number | null = null
  const cas: CasHandle = { state: () => st, setFloorOverride: (lo) => (floorOverride = lo) }
  const perf = makeProbe(false) as AwrPerf
  let now = 0
  const g = new PerfGovernor({ tier: () => tier, lowestAllowedRung: () => (tier === 'S' ? 0 : 2), perf, now: () => now })
  const applied: string[] = []
  const knob = (step: 1 | 2 | 3 | 4 | 5 | 6, id: string, levels: number): GovernorKnob => ({ step, id, levels, apply: (l) => applied.push(`${id}:${l}`) })
  const S = tier === 'S'
  for (const k of [knob(1, 'trails', 4), knob(2, 'frustums', S ? 2 : 3), knob(3, 'labels', 3), knob(4, 'lowpoly', 3), knob(5, 'env', S ? 2 : 3), knob(6, 'motion', S ? 2 : 3)]) g.registerKnob(k)
  g.setCas(cas)
  const tick = (ms: number, frozen = false): void => {
    now = ms
    const c: FrameCtx = { ...frameCtx, nowMs: ms, frozen }
    g.evaluate(c)
  }
  return { st, g, perf, applied, tick, floor: () => floorOverride }
}

describe('PerfGovernor (M06-AC-052)', () => {
  it('Tier S: degrade order, sub-steps, >= 2 s gaps, floor release last; reverse restore >= 10 s apart', () => {
    const { st, g, perf, applied, tick, floor } = setup('S')
    const steps: { t: number; step: number; dir: number }[] = []
    const off = events.on<{ step: number; dir: number }>('governor.step', (e) => steps.push({ t: -1, ...e }))
    // base: not saturated
    for (let t = 0; t <= 10_000; t += 1000) tick(t)
    expect(applied).toEqual([])
    // pressure: floor-saturated from 10 s
    for (let t = 10_000; t <= 70_000; t += 1000) {
      st.atFloorSinceMs = t - 10_000
      tick(t)
    }
    expect(applied).toEqual([
      'trails:1', 'trails:2', 'trails:3', 'frustums:1', 'labels:1', 'labels:2', 'lowpoly:1', 'lowpoly:2', 'env:1', 'motion:1',
    ])
    expect(floor()).toBe(10000)
    const hist = perf.governor.history
    expect(hist.map((h) => h.step)).toEqual([1, 1, 1, 2, 3, 3, 4, 4, 5, 6, 7])
    for (let i = 1; i < hist.length; i++) expect(hist[i].t - hist[i - 1].t).toBeGreaterThanOrEqual(2000)
    expect(hist[hist.length - 1].t).toBeLessThanOrEqual(60_000)
    expect(g.state).toBe('FLOOR_RELEASED')
    expect(g.step).toBe(7)
    // release pressure; ceiling saturated after 100 s
    st.atFloorSinceMs = 0
    const nBefore = hist.length
    for (let t = 71_000; t <= 250_000; t += 1000) {
      st.atCeilSinceMs = t >= 100_000 ? t - 100_000 : 0
      tick(t)
    }
    const rest = hist.slice(nBefore)
    expect(rest.every((h) => h.dir === 1)).toBe(true)
    expect(rest.map((h) => h.step)).toEqual([7, 6, 5, 4, 4, 3, 3, 2, 1, 1, 1].slice(0, rest.length))
    for (let i = 1; i < rest.length; i++) expect(rest[i].t - rest[i - 1].t).toBeGreaterThanOrEqual(10_000)
    expect(rest[0].t).toBeLessThanOrEqual(190_000)
    expect(floor()).toBeNull()
    expect(steps.length).toBe(hist.length)
    off()
  })

  it('hardware tiers wait for the CAS outer loop to reach the lowest allowed rung', () => {
    const { st, applied, tick } = setup('B')
    for (let t = 0; t <= 20_000; t += 1000) {
      st.atFloorSinceMs = t
      tick(t)
    }
    expect(applied).toEqual([]) // rung 4 > 2
    st.rung = 2
    for (let t = 21_000; t <= 24_000; t += 1000) {
      st.atFloorSinceMs = t
      tick(t)
    }
    expect(applied[0]).toBe('trails:1')
  })

  it('setCas with the same handle every tick keeps the step-7 knob and its level (FX-WEB1)', () => {
    const { st, g, tick, floor } = setup('S')
    // the host re-attaches the CAS in every governor tick; walk all the way down to the floor release
    for (let t = 0; t <= 80_000; t += 1000) {
      st.atFloorSinceMs = t
      tick(t)
    }
    expect(g.levelOf('pc.floor')).toBe(1)
    expect(floor()).toBe(st.lo)
    const cas = { state: () => st, setFloorOverride: () => {} }
    // a real re-attach of another handle resets the built-in knob; the same handle (host every tick) must not
    const same = (g as unknown as { cas: CasHandle }).cas
    g.setCas(same)
    g.setCas(same)
    expect(g.levelOf('pc.floor')).toBe(1)
    expect(g.knobLevels().filter((k) => k.step === 7)).toHaveLength(1)
    g.setCas(cas)
    expect(g.levelOf('pc.floor')).toBe(0)
    expect(g.hasCas).toBe(true)
    g.setCas(null)
    expect(g.hasCas).toBe(false)
    expect(g.knobLevels().some((k) => k.step === 7)).toBe(false)
  })

  it('frozen frames are not evaluated', () => {
    const { st, applied, tick } = setup('S')
    for (let t = 0; t <= 20_000; t += 1000) {
      st.atFloorSinceMs = 5000
      tick(t, true)
    }
    expect(applied).toEqual([])
  })

  it('a step whose knob reports no visible change is applied and recorded without a governor.step event (FX2-R2)', () => {
    const st: CasState = { atFloorSinceMs: 0, atCeilSinceMs: 0, B: 20000, lo: 10000, hi: 40000, rung: 0 }
    const cas: CasHandle = { state: () => st, setFloorOverride: () => {} }
    const perf = makeProbe(false) as AwrPerf
    let now = 0
    const g = new PerfGovernor({ tier: () => 'S', lowestAllowedRung: () => 0, perf, now: () => now })
    const applied: string[] = []
    let onScreen = false
    g.registerKnob({ step: 1, id: 'trails', levels: 2, apply: (l) => applied.push(`trails:${l}`), visible: () => onScreen })
    g.registerKnob({ step: 3, id: 'labels', levels: 2, apply: (l) => applied.push(`labels:${l}`), visible: () => true })
    g.setCas(cas)
    const ev: string[] = []
    const off = events.on<{ id: string; dir: number }>('governor.step', (e) => ev.push(`${e.id}:${e.dir}`))
    for (let t = 0; t <= 3000; t += 1000) {
      now = t
      st.atFloorSinceMs = t
      g.evaluate({ ...frameCtx, nowMs: t, frozen: false })
    }
    expect(applied).toEqual(['trails:1', 'labels:1'])
    const h = perf.governor.history
    expect(h.map((x) => x.step)).toEqual([1, 3])
    // FX2-R3: the invisible trails step chains into the labels step in the same evaluation (no 2 s wait for a change
    // that is not on screen); it is recorded as hidden
    expect(h[1].t).toBe(h[0].t)
    expect(h[0].reason).toBe('floor:trails:1:hidden')
    expect(h[1].reason).toBe('floor:labels:1')
    expect(ev).toEqual(['labels:-1']) // the invisible trails step raised no event (no Toast)
    expect(g.step).toBe(3)
    // restoring is silent for an invisible knob too, visible again: emitted
    onScreen = true
    st.atFloorSinceMs = 0
    for (let t = 5000; t <= 40_000; t += 1000) {
      now = t
      st.atCeilSinceMs = t - 5000
      g.evaluate({ ...frameCtx, nowMs: t, frozen: false })
    }
    expect(ev).toEqual(['labels:-1', 'labels:1', 'trails:1'])
    off()
  })
})

