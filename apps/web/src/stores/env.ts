// Environment panel and HUD summary (M07-FR-034, FR-058, FR-059, FR-061; M07 §8.1-§8.4; 14 §4.2, §6.14). Owner: M07.
// Written by viewport/layers/environment.tsx from EnvironmentRuntime.summary() at <= 4 Hz on Tier S and <= 10 Hz
// otherwise (M15-AC-021); the sub-layer switches (layers.*) and the quality setting are the only fields the UI writes
// (actions below), the adapter pushes them to the engine (engine/** never imports stores/**, AWR-03 §4.2).
// Field notes (M15 panel):
//   activePreset: selected toggle (keyframe target, else the preset the scalars match exactly); toPreset: target of the
//     current keyframe, the "pending" ring clears when toPreset equals the clicked id (the server confirmed it);
//   transition: sim-second window of the current smooth/exp keyframe and progress at tRender (panel "12 / 30 s");
//   scalars: server values at tRender (wind at 10 m reference height, from-direction, background MOR without
//     precipitation, nominal rain and snow, cloud cover 0-1); target: the same fields of the keyframe target; derived.morM is the headline total MOR, rainEffMmh the
//     cloud-gated effective rain; beaufort from the 10 m wind; selected: readings at the primary vehicle (EnvSample32
//     when available, else evaluated locally with the same formulas; stale when the vehicle pose is old);
//   state/staleS: EMPTY (skeleton), SYNCED, STALE (dashed values + "STALE x.x S"), EPOCH_WAIT (values held);
//   presetHashOk false when the keyframe carries another presets.json hash (warning bar; the store then evaluates
//     with the server copy); quality.level/reason for the "visual degraded (fog kept)" note; advanced: the collapsed
//     section; profileCurve: 31 heights 0-150 m of s f(z) and the gust envelope (<= 2 Hz); envSeries: 1 s buckets of
//     the selected vehicle wind over 120 s.
// Updates are batched: the adapter writes a new object only when a displayed value changed.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'

export type PresetId = 'clear' | 'partlyCloudy' | 'overcast' | 'lightRain' | 'rain' | 'heavyRain' | 'thunderstorm' | 'fog' | 'haze' | 'snow' | 'blizzard' | 'sandstorm'
export type EnvSyncState = 'EMPTY' | 'SYNCED' | 'STALE' | 'EPOCH_WAIT'
export type EnvQualityLevel = 'off' | 'low' | 'med'

export interface EnvSelected { windMps: number; speedMps: number; dirFromDeg: number; gustMps: number; morM: number; rainEffMmh: number; airspeedMps: number; stale: boolean; source: 'sample32' | 'local' }
export interface EnvAdvanced { turbSigmaRefMps: number; gustAmpMps: number; gustRateHz: number; wMeanMps: number; fogTopAglM: number; dust: number; level: number; turbModel: 'box' | 'dryden' | 'off'; turbAssetOk: boolean }
export interface EnvSeriesBucket { t: number; min: number; mean: number; max: number; n: number }

export interface EnvSummary {
  activePreset: PresetId | null
  toPreset: PresetId | null
  transition: { active: boolean; t0SimS: number; t1SimS: number; progress: number }
  scalars: { windSpeedRefMps: number; windDirFromDeg: number; morBgM: number; rainMmh: number; snowMmh: number; cloudCover: number }
  /** targets of the current keyframe (`to`): drafts may clear as soon as the server accepted them */
  target: { windSpeedRefMps: number; windDirFromDeg: number; morBgM: number; rainMmh: number; snowMmh: number; cloudCover: number }
  derived: { morM: number; rainEffMmh: number }
  beaufort: number
  /** readings at the selected vehicle (uav/{id}/env, 10 Hz, shown <= 4 Hz) */
  selected: EnvSelected | null
  layers: { precip: boolean; clouds: boolean; arrows: boolean; arrowsSliceAglM: 10 | 50 | 120; streamlines: boolean }
  presetHashOk: boolean
  state: EnvSyncState
  staleS: number
  version: number
  quality: { level: EnvQualityLevel; reason: 'governor' | 'user' | null; user: 'auto' | EnvQualityLevel }
  advanced: EnvAdvanced
  profileCurve: { t: number; v: number; env: number }[]
  envSeries: EnvSeriesBucket[]
}

const NAN_SCALARS = { windSpeedRefMps: Number.NaN, windDirFromDeg: Number.NaN, morBgM: Number.NaN, rainMmh: Number.NaN, snowMmh: Number.NaN, cloudCover: Number.NaN }

export const envStore = createAwrStore<EnvSummary>('env', () => ({
  activePreset: null, toPreset: null, transition: { active: false, t0SimS: 0, t1SimS: 0, progress: 0 },
  scalars: { ...NAN_SCALARS }, target: { ...NAN_SCALARS }, derived: { morM: Number.NaN, rainEffMmh: Number.NaN }, beaufort: 0, selected: null,
  layers: { precip: true, clouds: true, arrows: false, arrowsSliceAglM: 50, streamlines: false }, presetHashOk: true,
  state: 'EMPTY', staleS: 0, version: 0, quality: { level: 'low', reason: null, user: 'auto' },
  advanced: { turbSigmaRefMps: Number.NaN, gustAmpMps: Number.NaN, gustRateHz: Number.NaN, wMeanMps: Number.NaN, fogTopAglM: Number.NaN, dust: Number.NaN,
    level: 1, turbModel: 'box', turbAssetOk: true },
  profileCurve: [], envSeries: [],
}))

export function useEnv<T>(selector: (s: EnvSummary) => T): T {
  return useStore(envStore, selector)
}

/** UI actions: sub-layer switches (picture only, never physics, P-03) and the quality setting */
export const envLayers = {
  set(patch: Partial<EnvSummary['layers']>): void {
    envStore.setState({ layers: { ...envStore.getState().layers, ...patch } })
  },
  setQuality(user: 'auto' | EnvQualityLevel): void {
    envStore.setState({ quality: { ...envStore.getState().quality, user } })
  },
}

/** preset id -> icon registry key (15 §7.6 group C; env.blizzard falls back to env.snow until it is registered) */
export const PRESET_ICON: Readonly<Record<PresetId, string>> = {
  clear: 'env.clear', partlyCloudy: 'env.partly', overcast: 'env.overcast', lightRain: 'env.drizzle', rain: 'env.rain', heavyRain: 'env.storm',
  thunderstorm: 'env.thunder', fog: 'env.fog', haze: 'env.haze', snow: 'env.snow', blizzard: 'env.snow', sandstorm: 'env.sand',
}

/** Beaufort number of a 10 m wind speed (m/s) */
export function beaufortOf(v: number): number {
  const T = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]
  if (!Number.isFinite(v)) return 0
  let b = 0
  while (b < T.length && v >= T[b]) b++
  return b
}

/** MOR reading: "850 m" below 10 km, "12.0 km" from 10 km (the panel adds the MOR label and i18n) */
export function formatMor(m: number): { value: string; unit: 'm' | 'km' } {
  if (!Number.isFinite(m)) return { value: '', unit: 'm' }
  return m < 10_000 ? { value: String(Math.round(m)), unit: 'm' } : { value: (m / 1000).toFixed(1), unit: 'km' }
}

/** 16-point compass label of a from-direction (degrees) */
export function compass16(deg: number): string {
  const L = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
  if (!Number.isFinite(deg)) return ''
  return L[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16]
}
