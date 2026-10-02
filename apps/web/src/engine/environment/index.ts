// engine/environment facade (M07 §7.2; AWR-03 §4.1-§4.3). Owner: M07. engine/index.ts re-exports this module only.
// Pure functions mirror python/awr/environment line by line (golden parity, M07-AC-002); EnvStore evaluates the
// server-authoritative keyframes at tRender; EnvironmentRuntime owns the Low visuals, the shading provider and the
// quality knob; EnvBinding feeds the store from the realtime client.
export { F, NF, PRESETS_MODEL, PRESETS_SHA256, PresetsModel, presetsFromJson, type ProfileCfg } from './state/presets'
export { fromToUv, uvToFrom, eDir, nDir, shortestArc, enuToThree, threeToEnu, enuToNed, mod } from './state/conventions'
export { derive, newDerived, puddleTarget, smoothstep, clamp, DERIVED_KEYS, type EnvDerived } from './state/derive'
export { evalEnv, interp, makeTransition, expT1Ns, type Transition } from './state/evalEnv'
export { advance, partial, backstep, rates, anchorsInitial, H_NS } from './state/anchors'
export {
  decodeKeyframe, canon, turbUrl, weatherUrl, newAnchors, copyAnchors, MODE_STEP, MODE_SMOOTH, MODE_EXP,
  type EnvKeyframe, type EnvKeyframeWire, type Anchors, type GustEv,
} from './state/keyframe'
export { EnvStore, QUEUE_MAX, MAX_STEPS_PER_FRAME, type EnvSyncState } from './state/EnvStore'
export { isa, sectorSlots } from './state/isa'
export { opticalDepth, flatLen, sigmaAt, kimQ, sigmaLambda, lidarTwoWay, H_HAZE } from './atmosphere/optics'
export { fogFactorNode, opticalDepthNode, sceneFogNode, transmittanceNode } from './atmosphere/fogNode'
export { profile, profileCfg, fAdv } from './wind/profile'
export { gust, gustExpired, gustCreate } from './wind/gust'
export { windCPU, type WindCpuOpts } from './wind/windCPU'
export { windAtEnu, profileNode } from './wind/windNode'
export { Streamlines, decodeAWSL, DASH_M, type AwslSet } from './wind/Streamlines'
export { decodeAWRV, volumeTexture3D, volumeTexture2D, crc32, halfToFloat, type AwrvVolume } from './wind/awrv'
export { TurbBoxCPU, milSigma } from './wind/turbBox'
export { EnvParams, makeEnvNodes, horizonColor, type EnvNodes } from './lighting/EnvUniforms'
export { createEnvShading, type SceneShadingProvider } from './lighting/EnvShading'
export { EnvTerrain, type DtmSource } from './terrain/dtmSampler'
export { EnvQuality, type EnvUserLevel } from './quality/EnvQuality'
export { ENV_TIERS, type EnvLevel } from './quality/envTiers'
export { PrecipAnchor } from './precip/PrecipAnchor'
export { EnvironmentRuntime, arrowSpacingIdx, type EnvSubLayers, type EnvPerf } from './EnvRuntime'
export { EnvBinding } from './binding'
export { buildSummary, localReadings, sampleReadings, EnvSampleCache, ES32, profileCurve, EnvSeries, beaufort, type EnvSummaryData, type EnvSelectedData } from './summary'
