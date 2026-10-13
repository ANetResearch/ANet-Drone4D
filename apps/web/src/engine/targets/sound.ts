// Sound source waveform for the form chart (M18-FR-025, P1; AWR-04 §7.5; ADR-108: display only, never fed back to the
// criteria). Owner: M18. One period of s(t) = sum_k a_k sin(2 pi k f0 t) gate(t): a_k from harmonics_db (relative to the
// fundamental), gate = 1 in the on phase ((t - phase_s) mod period_s < duty * period_s), 0.01 (-40 dB) in the off phase.
// The carrier is drawn at a display frequency (f0 scaled so that about 8 cycles fit a 1 s window) because a 200 Hz tone
// over an 8 s period is not readable at chart resolution; the gate (period, duty, phase) is exact.

export interface SoundParams {
  waveform: 'tone' | 'harmonic' | 'pulse_train' | 'noise_band'
  f0_hz: number
  harmonics_db?: readonly number[]
  lw_dba: number
  period_s: number
  duty: number
  phase_s?: number
  bandwidth_hz?: number
}

export const OFF_GAIN = 0.01

export function gateOn(s: SoundParams, tS: number): boolean {
  if (s.duty >= 1) return true
  if (s.duty <= 0) return false
  const ph = (((tS - (s.phase_s ?? 0)) % s.period_s) + s.period_s) % s.period_s
  return ph < s.duty * s.period_s
}

/** nSamples over one period [0, period_s) into out (amplitude normalised to the on-phase peak, in [-1, 1]) */
export function soundPeriodSamples(s: SoundParams, nSamples: number, out: Float32Array): void {
  const n = Math.min(nSamples, out.length)
  const harm = s.waveform === 'tone' || s.waveform === 'noise_band' ? [0] : (s.harmonics_db ?? [0, -6, -10, -14])
  const amps = harm.map((db) => 10 ** (db / 20))
  const cycles = Math.max(4, Math.min(64, Math.round(8 * s.period_s)))
  const fDisp = cycles / s.period_s
  let peak = 0
  for (let i = 0; i < n; i++) {
    const t = (i / n) * s.period_s
    let x = 0
    if (s.waveform === 'noise_band') {
      // deterministic pseudo-noise (sum of incommensurate sines), display only
      x = Math.sin(2 * Math.PI * fDisp * t) * 0.6 + Math.sin(2 * Math.PI * fDisp * 1.618 * t + 1) * 0.4 + Math.sin(2 * Math.PI * fDisp * 2.414 * t + 2) * 0.3
    } else {
      for (let k = 0; k < amps.length; k++) x += amps[k] * Math.sin(2 * Math.PI * (k + 1) * fDisp * t)
    }
    x *= gateOn(s, t) ? 1 : OFF_GAIN
    out[i] = x
    peak = Math.max(peak, Math.abs(x))
  }
  if (peak > 0) for (let i = 0; i < n; i++) out[i] /= peak
}

/** on-phase intervals of one period (s): for the chart's shaded bands */
export function onIntervals(s: SoundParams): [number, number][] {
  if (s.duty >= 1) return [[0, s.period_s]]
  if (s.duty <= 0) return []
  const a = (((s.phase_s ?? 0) % s.period_s) + s.period_s) % s.period_s
  const b = a + s.duty * s.period_s
  return b <= s.period_s ? [[a, b]] : [[0, b - s.period_s], [a, s.period_s]]
}
