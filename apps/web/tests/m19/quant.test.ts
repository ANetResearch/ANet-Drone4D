// M19-AC-021 (Node part), D2-AC-52: the TS port of the pair quantities equals the Python implementation on the golden
// fixture quant_cases.json (tests/perception/test_field_golden.py): arrival level, margin and threshold <= 0.1 dB, angular
// size, equivalent pixels and apparent contrast <= 1 %, radar SNR <= 0.1 dB, probabilities <= 0.01; the single-band
// anchor 145.7 m gives P = 0.5; the default band shape and tonal masking of P600 match; the columnar topic form decodes to
// exactly the expanded REST form.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  acousticPair, acousticRadius, decodeTargetQuant, defaultBandShape, pAny, pLogistic, pVisual, radarPair, tonalMasking, visualPair,
  type AcousticPairInput, type RadarRx, type VisualPairInput,
} from '@/engine/perception'

const FIX = JSON.parse(readFileSync(join(import.meta.dirname, 'quant_cases.json'), 'utf8')) as {
  acoustic: { in: AcousticPairInput; expect: Record<string, number> }[]
  visual: { in: VisualPairInput; expect: Record<string, number | boolean | null> }[]
  radar: { rx: RadarRx; rcs: number; rM: number; rain: number; inFov: boolean; expect: Record<string, number> }[]
  single_band: { rM: number; expect: number }[]
  p600_shape: number[]
  p600_masking: number[]
  radii: { lwBands: number[]; thr: number[]; gBackDb: number; expect: number }[]
  soa: { topic: unknown; expanded: unknown }
}

const rel = (a: number, b: number, r: number): boolean => Math.abs(a - b) <= Math.max(1e-9, r * Math.max(Math.abs(a), Math.abs(b)))

describe('pair quantities vs Python (M19-AC-021)', () => {
  it('display probabilities are calibrated at the criterion boundary', () => {
    expect(pLogistic(0, 2)).toBe(0.5)
    expect(pLogistic(2 * Math.log(9), 2)).toBeCloseTo(0.9, 12)
    expect(pVisual(1)).toBe(0.5)
    expect(pVisual(1.75)).toBeCloseTo(0.9, 2)
    expect(pVisual(0)).toBe(0)
    expect(pAny([0.5, 0.5])).toBe(0.75)
  })

  it('200 acoustic cases within 0.1 dB', () => {
    expect(FIX.acoustic.length).toBe(200)
    for (const c of FIX.acoustic) {
      const q = acousticPair(c.in)
      expect(Math.abs(q.lpDba - c.expect.lpDba)).toBeLessThanOrEqual(0.1)
      expect(Math.abs(q.lpBandDb - c.expect.lpBandDb)).toBeLessThanOrEqual(0.1)
      expect(Math.abs(q.thrDba - c.expect.thrDba)).toBeLessThanOrEqual(0.1)
      expect(Math.abs(q.marginDb - c.expect.marginDb)).toBeLessThanOrEqual(0.1)
      expect(Math.abs(q.lBgDba - c.expect.lBgDba)).toBeLessThanOrEqual(0.1)
      expect(q.bandHz).toBe(c.expect.bandHz)
      expect(Math.abs(q.p - c.expect.p)).toBeLessThanOrEqual(0.01)
    }
  })

  it('single-band anchor: P600 at 145.7 m (rural night, a 0.5) has P = 0.50', () => {
    for (const s of FIX.single_band) {
      const q = acousticPair({ lwBands: [88], rM: s.rM, lBgBands: [30], masking: [0], hearingThDba: 20, alertness: 0.5, advDb: 0, alpha: [5], aW: [0],
        bandsHz: [1000] })
      expect(q.lpDba).toBeCloseTo(s.expect, 9)
      if (s.rM === 145.7) expect(Math.abs(q.p - 0.5)).toBeLessThanOrEqual(0.01)
    }
  })

  it('200 visual cases within 1 %', () => {
    expect(FIX.visual.length).toBe(200)
    for (const c of FIX.visual) {
      const q = visualPair(c.in)
      for (const k of ['thetaArcmin', 'dVisM', 'thetaThArcmin', 'ratio', 'cApp'] as const) expect(rel(q[k], c.expect[k] as number, 0.01)).toBe(true)
      if (c.expect.nEqPx === null) expect(q.nEqPx).toBeNull()
      else expect(rel(q.nEqPx as number, c.expect.nEqPx as number, 0.01)).toBe(true)
      expect(Math.abs(q.p - (c.expect.p as number))).toBeLessThanOrEqual(0.01)
      expect(q.ok).toBe(c.expect.ok)
    }
  })

  it('100 radar cases within 0.1 dB', () => {
    for (const c of FIX.radar) {
      const q = radarPair(c.rx, c.rcs, c.rM, c.rain, c.inFov)
      expect(Math.abs(q.snrDb - c.expect.snrDb)).toBeLessThanOrEqual(0.1)
      expect(Math.abs(q.p - c.expect.p)).toBeLessThanOrEqual(0.01)
    }
  })

  it('default band shape, tonal masking and the effective acoustic radius', () => {
    const s = defaultBandShape(150, 12, 1414)
    s.forEach((v, b) => expect(Math.abs(v - FIX.p600_shape[b])).toBeLessThanOrEqual(1e-6))
    expect(tonalMasking(150)).toEqual(FIX.p600_masking)
    for (const c of FIX.radii) expect(Math.abs(acousticRadius(c.lwBands, c.thr, c.gBackDb) - c.expect)).toBeLessThanOrEqual(1.0)
  })

  it('columnar soa1 decodes to exactly the expanded form', () => {
    expect(decodeTargetQuant(FIX.soa.topic)).toEqual(FIX.soa.expanded)
    expect(decodeTargetQuant(FIX.soa.expanded)).toEqual(FIX.soa.expanded)
    expect(decodeTargetQuant(null)).toBeNull()
    expect(decodeTargetQuant({ enc: 'soa1' })?.pairs).toEqual([])
  })
})
