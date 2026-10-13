// viewport/sensorView: FPV sensor views EO, night (NIR) and LWIR (D2 WP-11; AWR-04 §10.5; AWR-14 §20.10.5). Owner: M06.
// ui/** reaches it through viewport/facade.ts (`sensorView`); the backend through pass.ts.
export * from './math'
export {
  DEFAULT_ENV, EMITTER, EMITTER_STRIDE, availableNow, cycleSensorView, selectSensor, sensorViewActive, sensorViewInfo, setDigitalZoom, setEmitterSource,
  setPalette, setSensorEnv, setSensorView, sv, type EmitterSource, type SensorEnv, type SensorViewInfo, type SensorViewResult,
} from './state'
export { SensorViewPass, drawsIn, sensorClassOf, type SensorClass } from './pass'
export { projectBox } from './hud'
