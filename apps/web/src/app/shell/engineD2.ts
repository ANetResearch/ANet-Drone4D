// D2 engine modules for the UI shell (TS-BND-01, AWR-03 §4.2): ui/** reaches the engine only through the engine facade
// (engine/index.ts, M06). The D2 modules engine/perception (M19: calculator, frame decoding) and engine/targets (M18: class
// templates, the page's target model) are not on the facade yet (their `export *` collides on LevelName); this module is
// the single interim entry of the UI to them, re-exporting only what the panels use. Wiring point: once the facade carries
// them (request d2-WP-12b-to-M06 item 1) the imports below become `from '@/engine'` and nothing else changes.
export {
  defaultEnv, feasibilityDetail, gsdAtRange, gsdM, gx40, lwir640, maxRangeForLevel, N50_PX, N_P90, perceptionLevel, pPrimeUm, sensorFromCatalog,
  sensorSummary, targetFromClass, ttpf,
} from '@/engine/perception'
export type { Light, PerceptionSensorFrame, PerceptionTarget, ReqLevel, SensorP } from '@/engine/perception'
export { CLASS_DEFAULTS, soundPeriodSamples, TARGET_CLASSES, targetsModel } from '@/engine/targets'
export type { BodyView, TargetCfg, TargetClassName } from '@/engine/targets'
