// Engine facade (AWR-03 §4.1-§4.3; M06 §7.1). Owner: M06. ui/** reaches the engine only through this module
// (TS-BND-01). It only re-exports the engine modules (engine/<mod>/index.ts); modules register phases through
// loop.register and layers through viewport/layers/registry.ts.
export * from './loop'
export * from './geo/index'
export * from './labels/index'
export * from './perf/index'
export * from './anim/index'
export * from './time/index'
export * from './pointcloud/index'
export * from './drones/index'
export * from './camera/index'
export * from './picking/index'
export * from './mission/index'
