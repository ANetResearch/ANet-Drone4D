// engine/time facade (M12 §7.1, §9.1). Owner: M12. Pure TS: no React, no ui/**, no stores/** (AWR-03 §4.2 item 2).
// The telemetry task of M06 hands each TelemetryFrame to ingest(); the clock phase task ('m12.clock') computes the one
// render time tRender = simNow - D_global (and tFocus for the Third/FPV vehicle, ADR-046) into the frame context;
// sampleSwarm / sampleOne give the interpolated poses. Timeline logic (pending controls, guards, step targets, track
// model, sidecar parsers) is here too so stores/timeline.ts and M15's panels stay thin.
export { SimClockView, type ClockView, type TimeFrame } from './clockView'
export { DelayController, ArrivalStats, WallDelay, SimDelay } from './delay'
export { InterpRing, newDronePoseSoA, type DronePoseSoA, type Interp } from './interpRing'
export { hermite3, hermiteRing, hermiteBasis, slerpInto, integrateOmegaInto } from './hermite'
export { TIME_PARAMS, TS as TIME_STATE, advancesIn, TICK_MS } from './params'
export { onEpoch, onReset, epochBus, type ResetRange } from './epochBus'
export { initTime, timeRuntime, type TimeRuntime, type TimeDeps } from './register'
export { perfTime, installPerfTime, type PerfTime } from './perfTime'
export { StepGlide } from './stepGlide'
export {
  PendingMachine, RtfWatch, guards, reasonKey, replayStepKind, replayStepTarget, liveStepMs, LIVE_STEP_TICKS,
  type Control, type Guards, type GuardInput, type Pending, type PendingOutcome, type StepKind,
} from './playerController'
export { MarkerClass, MARKER_RULES, MARKER_PRIORITY, MARKER_SUPERSEDED, MARKER_CLASS_MASK, markerClassOf, type MarkerRule } from './markers'
export { TrackModel, ticks as timelineTicks, type ColumnAgg, type MarkerColumns, type RangeKind, type TrackRange } from './trackModel'
export { parseEvx, parseOvw, ovwBinMs, EVX, OVW, OVW_BIN, type EvxIndex, type OvwIndex } from './sidecars'
