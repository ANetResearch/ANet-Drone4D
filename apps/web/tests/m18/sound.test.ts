// M18-AC-021 (P1): the waveform chart synthesises one period consistent with the parameters (period, duty, phase): the
// on-phase spans duty x period starting at phase_s, the off phase is -40 dB, the amplitude is normalised.
import { describe, expect, it } from 'vitest'
import { gateOn, OFF_GAIN, onIntervals, soundPeriodSamples, type SoundParams } from '@/engine/targets'

describe('sound waveform (M18-AC-021)', () => {
  const s: SoundParams = { waveform: 'pulse_train', f0_hz: 600, harmonics_db: [0], lw_dba: 100, period_s: 6, duty: 0.15, phase_s: 1 }
  it('gate and on intervals', () => {
    expect(gateOn(s, 1.0)).toBe(true)
    expect(gateOn(s, 1.89)).toBe(true)
    expect(gateOn(s, 1.91)).toBe(false)
    expect(gateOn(s, 0.5)).toBe(false)
    expect(onIntervals(s)).toEqual([[1, 1 + 0.15 * 6]])
    const wrap = onIntervals({ ...s, phase_s: 5.5 })
    expect(wrap[0][0]).toBe(0)
    expect(wrap[0][1]).toBeCloseTo(0.4, 9)
    expect(wrap[1]).toEqual([5.5, 6])
    expect(onIntervals({ ...s, duty: 1 })).toEqual([[0, 6]])
  })
  it('one period of samples', () => {
    const n = 1200
    const out = new Float32Array(n)
    soundPeriodSamples(s, n, out)
    let peakOn = 0
    let peakOff = 0
    for (let i = 0; i < n; i++) {
      const t = (i / n) * s.period_s
      if (gateOn(s, t)) peakOn = Math.max(peakOn, Math.abs(out[i]))
      else peakOff = Math.max(peakOff, Math.abs(out[i]))
    }
    expect(peakOn).toBeCloseTo(1, 3)
    expect(peakOff).toBeLessThanOrEqual(OFF_GAIN * 1.01)
    const h = new Float32Array(400)
    soundPeriodSamples({ waveform: 'harmonic', f0_hz: 220, harmonics_db: [0, -6, -10, -14], lw_dba: 83, period_s: 1, duty: 1 }, 400, h)
    expect(Math.max(...Array.from(h, Math.abs))).toBeCloseTo(1, 3)
  })
})
