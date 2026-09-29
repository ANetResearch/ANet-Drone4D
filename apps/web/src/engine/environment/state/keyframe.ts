// EnvKeyframe wire decoding and canonical encoding (M07-FR-001; M07 §6.2.5; ADR-025). Owner: M07.
// Wire: msgpack snake_case, EnvScalars position-encoded (21 numbers, readers accept longer arrays), route intermediate
// states as preset ids, shared asset URLs derived from seed and parameters (never on the wire). Canonical numbers:
// integer values (|x| < 2^53) as the shortest integer, others float64, -0 as 0; @msgpack/msgpack 3.1.3 encodes numbers
// exactly so, therefore Python encode -> TS decode -> TS encode is byte-identical (M07-AC-001; msgpack itself stays in
// net/**, the round trip is checked in tests/environment/keyframe.test.ts). Decoding happens in EnvStore.ingest (a few
// Hz, may allocate); the per-frame path only reads the decoded object.
import { F, NF, PRESETS_MODEL, type PresetsModel, type ProfileCfg } from './presets'

export const MODE_STEP = 0
export const MODE_SMOOTH = 1
export const MODE_EXP = 2

export interface AnchorsWire { t_ns: number; s_m: number; d_enu_m: number[]; fall_rain_m: number; fall_snow_m: number; wetness: number; puddle: number }
export interface EnvKeyframeWire {
  schema: 'awr.env.keyframe.v1'
  world_id: string
  version: number
  epoch: number
  seed: number
  t_ns: number
  t_apply_ns: number
  config: {
    wind: { level: number; profile: ProfileCfg; library: unknown; turbulence: { model: 'box' | 'dryden' | 'off'; n: number; dx_m: number; l_m: number }; gust: { model: string; max_active: number } }
    sun: { azimuth_deg: number; elevation_deg: number }
    weather_map: { n: number; scale_m: number }
    presets_sha256: string
  }
  mode: 'smooth' | 'exp' | 'step'
  t0_ns: number
  t1_ns: number
  from: number[]
  to: number[]
  via: string[]
  to_preset: string | null
  anchors: AnchorsWire
  events: number[][]
  vis: { streamlines: string | null; vmax_mps: number }
}

/** gust front (M07 §6.2.4), fields in wire order */
export interface GustEv { kind: number; id: number; tCreateNs: number; x0: number; s0: number; amp: number; lam: number; dirFromDeg: number; sSpan: number }

export interface Anchors { tNs: number; sM: number; d: Float64Array; fallRain: number; fallSnow: number; wetness: number; puddle: number }
export function newAnchors(): Anchors {
  return { tNs: 0, sM: 0, d: new Float64Array(3), fallRain: 0, fallSnow: 0, wetness: 0, puddle: 0 }
}
export function copyAnchors(src: Anchors, dst: Anchors): Anchors {
  dst.tNs = src.tNs
  dst.sM = src.sM
  dst.d[0] = src.d[0]
  dst.d[1] = src.d[1]
  dst.d[2] = src.d[2]
  dst.fallRain = src.fallRain
  dst.fallSnow = src.fallSnow
  dst.wetness = src.wetness
  dst.puddle = src.puddle
  return dst
}
export function anchorsFromWire(a: AnchorsWire, out: Anchors = newAnchors()): Anchors {
  out.tNs = Number(a.t_ns)
  out.sM = a.s_m
  out.d[0] = a.d_enu_m[0]
  out.d[1] = a.d_enu_m[1]
  out.d[2] = a.d_enu_m[2]
  out.fallRain = a.fall_rain_m
  out.fallSnow = a.fall_snow_m
  out.wetness = a.wetness
  out.puddle = a.puddle
  return out
}

/** decoded keyframe; `route`/`routeEnter` are built once (the smooth route with its enter/leave windows) */
export interface EnvKeyframe {
  readonly wire: EnvKeyframeWire
  readonly P: PresetsModel
  readonly worldId: string
  readonly version: number
  readonly epoch: number
  readonly seed: number
  readonly tNs: number
  readonly tApplyNs: number
  readonly mode: 0 | 1 | 2
  readonly t0Ns: number
  readonly t1Ns: number
  readonly from: Float64Array
  readonly to: Float64Array
  readonly via: readonly string[]
  readonly toPreset: string | null
  readonly anchors: Anchors
  readonly events: readonly GustEv[]
  readonly level: number
  readonly profile: ProfileCfg
  readonly turbModel: 'box' | 'dryden' | 'off'
  readonly route: readonly Float64Array[]
  /** per route segment: 1 = enter window (precipitation rises), 0 = leave */
  readonly routeEnter: Uint8Array
}

function vec(a: readonly number[]): Float64Array {
  const v = new Float64Array(NF)
  for (let i = 0; i < NF; i++) v[i] = +a[i]
  return v
}

export function precipLevel(s: ArrayLike<number>): number {
  return s[F.RAIN] + 10.0 * s[F.SNOW]
}

export function decodeKeyframe(w: EnvKeyframeWire, P: PresetsModel = PRESETS_MODEL): EnvKeyframe {
  if (w.schema !== 'awr.env.keyframe.v1') throw new Error(`not an EnvKeyframe: ${String(w.schema)}`)
  const from = vec(w.from)
  const to = vec(w.to)
  const via = w.via.map(String)
  const route = [from, ...via.map((id) => P.overlay(to, id, new Float64Array(NF))), to]
  const routeEnter = new Uint8Array(route.length - 1)
  for (let i = 0; i < route.length - 1; i++) routeEnter[i] = precipLevel(route[i + 1]) > precipLevel(route[i]) ? 1 : 0
  const mode = w.mode === 'step' ? MODE_STEP : w.mode === 'exp' ? MODE_EXP : MODE_SMOOTH
  return {
    wire: w, P, worldId: w.world_id, version: Number(w.version), epoch: Number(w.epoch), seed: Number(w.seed), tNs: Number(w.t_ns),
    tApplyNs: Number(w.t_apply_ns), mode, t0Ns: Number(w.t0_ns), t1Ns: Number(w.t1_ns), from, to, via, toPreset: w.to_preset ?? null,
    anchors: anchorsFromWire(w.anchors),
    events: w.events.map((e) => ({ kind: e[0], id: e[1], tCreateNs: Number(e[2]), x0: e[3], s0: e[4], amp: e[5], lam: e[6], dirFromDeg: e[7], sSpan: e[8] })),
    level: w.config.wind.level, profile: w.config.wind.profile, turbModel: w.config.wind.turbulence.model, route, routeEnter,
  }
}

const SAFE = 2 ** 53

/** canonical-number preprocessing (the Python side does the same before msgpack) */
export function canon(x: unknown): unknown {
  if (typeof x === 'number') {
    if (x === 0) return 0
    return x
  }
  if (Array.isArray(x)) return x.map(canon)
  if (x !== null && typeof x === 'object') {
    const o: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(x)) o[k] = canon(v)
    return o
  }
  return x
}

/** shared asset URLs derived from seed and parameters (M07 §6.2.2; 16 §8.4) */
export function turbUrl(seed: number, n: number, dx: number, l: number): string {
  return `/worlds/_shared/env/turb/vk_s${seed}_n${n}_dx${dx}_L${l}.awrv`
}
export function weatherUrl(seed: number, n: number): string {
  return `/worlds/_shared/env/weather/weather_s${seed}_${n}.awrv`
}
export { SAFE as SAFE_INT }
