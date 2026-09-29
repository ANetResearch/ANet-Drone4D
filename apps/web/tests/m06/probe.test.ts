// M06-AC-050 (AWR-18 §9; M06 §6.18): the window.__perf snapshot validates against awr.perf.v1 (Ajv strict, draft
// 2020-12) with and without ring contents; rings are fixed 65536-capacity buffers; governor history and LoAF worst
// entries reuse pooled objects; reset scopes; inject exists only in test builds; LoAF attribution of our chunks and of
// the loop callback (by start time); FrameSampler fastest-frame refresh estimate and overBudgetFrames.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import Ajv2020 from 'ajv/dist/2020'
import { describe, expect, it } from 'vitest'
import { cbStarts, layerMs, PERF_LAYERS } from '@/engine/loop'
import { makeProbe, pushGovernorHistory, pushLoafWorst, pushRing, RING_CAP, tailRing, type AwrPerf } from '@/engine/perf/probe'
import { feedInterval, frameEnd, frameStart, refreshMs, sampler, setSoftware } from '@/engine/perf/frameSampler'
import { isLoopCallback, isOurScript, oursOutsideLoop } from '@/engine/perf/loaf'

const SCHEMA = JSON.parse(readFileSync(join(import.meta.dirname, '../../../../packages/contracts/perf/perf-snapshot.schema.json'), 'utf8'))

describe('__perf probe (M06-AC-050)', () => {
  it('snapshot validates against awr.perf.v1 (Ajv strict)', () => {
    const p = makeProbe(true) as AwrPerf
    for (let i = 0; i < 100; i++) pushRing(p.frame.interval, 33.3)
    pushGovernorHistory(p, 1000, 1, -1, 'floor:trails:1')
    pushLoafWorst(p, 80, 60, 10, 'FrameRequestCallback')
    const ajv = new Ajv2020({ strict: true, allErrors: true })
    const validate = ajv.compile(SCHEMA)
    for (const rings of [false, true]) {
      const s = p.snapshot({ rings })
      const ok = validate(s)
      expect(ok, JSON.stringify(validate.errors)).toBe(true)
      expect(JSON.parse(JSON.stringify(s)).schema).toBe('awr.perf.v1')
    }
    expect((p.snapshot({ rings: true }).frame as { interval: number[] }).interval.length).toBe(100)
    expect(typeof p.inject).toBe('function')
    expect((makeProbe(false) as AwrPerf).inject).toBeUndefined()
  })

  it('rings wrap at 65536, pools are bounded, reset clears the frame scope', () => {
    const p = makeProbe(false) as AwrPerf
    expect(p.frame.interval.buf.length).toBe(RING_CAP)
    for (let i = 0; i < RING_CAP + 10; i++) pushRing(p.frame.interval, i)
    const out = new Float64Array(3)
    expect(tailRing(p.frame.interval, 3, out)).toBe(3)
    expect(Array.from(out)).toEqual([RING_CAP + 7, RING_CAP + 8, RING_CAP + 9])
    for (let i = 0; i < 100; i++) pushGovernorHistory(p, i, 1, -1, 'x')
    expect(p.governor.history.length).toBe(64)
    for (let i = 0; i < 20; i++) pushLoafWorst(p, i, 0, 0, '')
    expect(p.loaf.worst.map((w) => w.durationMs)).toEqual([19, 18, 17, 16, 15, 14, 13, 12])
    p.reset('frame')
    expect(p.frame.interval.n).toBe(0)
    expect(p.loaf.worst.length).toBe(0)
  })

  it('FrameSampler: intervals, fastest-frame refresh estimate, layer budgets, loop part of LoAF', () => {
    const p = makeProbe(false) as AwrPerf
    setSoftware(false)
    for (let t = 0; t < 3000; t += 16.667) {
      frameStart(p, t)
      feedInterval(16.667)
    }
    expect(p.frame.interval.n).toBeGreaterThan(100)
    expect(Math.abs(refreshMs() - 16.667)).toBeLessThan(0.5)
    setSoftware(true)
    expect(refreshMs()).toBe(33.3)
    layerMs.fill(0)
    layerMs[PERF_LAYERS.indexOf('labels')] = 5 // > 2 ms budget
    frameEnd(p)
    expect(p.layers.labels.overBudgetFrames).toBe(1)
    expect(sampler.overMask & (1 << PERF_LAYERS.indexOf('labels'))).not.toBe(0)
  })

  it('LoAF attribution: our chunks, not vendor chunks; the loop callback is recognised by start time', () => {
    const origin = 'http://127.0.0.1:4173'
    expect(isOurScript(`${origin}/assets/engine-Bq1.js`, origin)).toBe(true)
    expect(isOurScript(`${origin}/assets/index-CPjx7sdf.js`, origin)).toBe(true)
    expect(isOurScript(`${origin}/assets/ui-DnpAXJeW.js`, origin)).toBe(true)
    expect(isOurScript(`${origin}/assets/three-Hw3bG6Ri.js`, origin)).toBe(false)
    expect(isOurScript(`${origin}/assets/react-C-eD.js`, origin)).toBe(false)
    expect(isOurScript('https://other/assets/engine-x.js', origin)).toBe(false)
    cbStarts[0] = 1000
    expect(isLoopCallback({ invokerType: 'frame-request-callback', startTime: 1000.3, duration: 60 })).toBe(true)
    expect(isLoopCallback({ invokerType: 'event-listener', startTime: 1000.3, duration: 60 })).toBe(false)
    const e = {
      duration: 120, startTime: 900, scripts: [
        { invokerType: 'frame-request-callback', startTime: 1000.2, duration: 70, sourceURL: `${origin}/assets/index-a.js` },
        { invokerType: 'event-listener', invoker: 'DIV.onclick', startTime: 1080, duration: 55, sourceURL: `${origin}/assets/ui-b.js` },
        { invokerType: 'event-listener', startTime: 1140, duration: 30, sourceURL: `${origin}/assets/three-c.js` },
      ],
    }
    expect(oursOutsideLoop(e, origin)).toEqual({ ours: 55, invoker: 'DIV.onclick' })
  })
})
