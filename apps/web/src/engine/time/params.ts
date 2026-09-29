// Control-law timers and constants of the client clock (M12 §6.2 timer table, §6.3 parameter table, §6.10; ADR-045,
// ADR-046). Owner: M12. These are clock, delay and protocol control constants registered as ADR-045 timers (M12 F-21),
// not presentation motion: presentation durations (freeze decay, single-step glide) come from lib/tokens/motion.gen.ts.
// Every entry maps to one row of the M12 §6.2 timer table or the §6.10 defaults.
export const TIME_PARAMS = {
  /** simNow may run ahead of the last TIME by at most rate x 1 s (AWR-17 §6.10) */
  extrapCapMs: 1000,
  /** snap threshold 0.25 s x max(rate, 1) and convergence time constant 250 ms (wall clock) */
  snapMs: 250,
  convergeTauMs: 250,
  /** TIME older than this marks the clock stale (AWR-17 §10.7) */
  staleMs: 1000,
  /** D_wall clamp, smoothing time constant, rate limit (per second) and hysteresis (ADR-046) */
  dMinMs: 60,
  dMaxMs: 300,
  dTauMs: 1000,
  dRatePerS: 0.1,
  dHyst: 0.05,
  /** arrival intervals kept for jitter_p95 (about 3 s at 10 Hz) */
  jitterSamples: 32,
  /** hz_eff counts new samples in this wall-clock window (arrival ring sized for the 60 Hz focus channel) */
  hzWindowMs: 1000,
  hzArrivals: 128,
  /** EMA weight of each new hz_eff reading */
  hzEma: 0.25,
  /** recovery ramp and rate transition of D_sim, linear (wall clock) */
  rampMs: 1000,
  /** entering and leaving the focus exception (Third/FPV): dFocusSim blends between dSim and rate x D_focus_wall */
  focusBlendMs: 300,
  /** extrapolation limit: 3 sample intervals (ADR-046) */
  extrapIntervals: 3,
  /** default recording block interval (x1 recording, 25 Hz), used as the extrapolation floor after a frozen seek */
  blockIntervalMs: 40,
  /** interpolation ring depth per vehicle and capacity step (M12 §6.4) */
  ringK: 32,
  ringStep: 256,
  /** single-step glide only when the step is at most 1 s of simulation time (FR-013) */
  glideMaxStepMs: 1000,
  /** pending confirmation of a transport control (wall clock, AWR-14 §6.17) */
  pendingTimeoutMs: 1000,
  /** seek feedback: no playbackState{did_seek} within this time shows BUFFERING (wall clock, r15 §3.7) */
  bufferingFeedbackMs: 100,
  /** RTF badge: actual < 0.95 x requested for 2 s shows it, 2 s of recovery hides it (FR-015) */
  rtfRatio: 0.95,
  rtfHoldMs: 2000,
  /** timeline store write rate: Tier S 4 Hz, others 10 Hz (FR-020) */
  storeHzS: 4,
  storeHzBA: 10,
  /** selected vehicle altitude series sampling (Hz) and ring length (1 h at 4 Hz) */
  seriesHz: 4,
  seriesCap: 14400,
  /** marker store cap; INFO markers are dropped first beyond it (FR-018) */
  markersCap: 500_000,
  /** bookmarks per run (FR-026) and label length */
  bookmarksCap: 1000,
  bookmarkLabelMax: 64,
  /** zoom limit of the detailed track (FR-024) */
  minSpanS: 2,
  /** window over which __perf.time ratios are measured (1 s) and the p95 window of the interpolation costs (10 s) */
  perfWindowMs: 1000,
  perfCostWindow: 600,
} as const

/** TIME.state values (AWR-03 §5.2 item 6; AWR-17 §6.4), read after masking bit 7 */
export const TS = {
  STOPPED: 0, PLAYING: 1, PAUSED: 2, STEPPING: 3, BUFFERING: 4, ENDED: 5, STALLED: 6, RESTARTING: 7, FAILED: 8, LIVE: 9,
} as const
/** the clock advances only in PLAYING and LIVE (AWR-03 appendix D E-15) */
export const advancesIn = (state4: number): boolean => state4 === TS.PLAYING || state4 === TS.LIVE
/** sim/step tick length (M08 SimClock, 250 Hz) */
export const TICK_MS = 4
