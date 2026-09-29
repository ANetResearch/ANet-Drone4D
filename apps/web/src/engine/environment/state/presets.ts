// Environment presets model (M07-FR-002, FR-008; M07 §6.2.1, §6.5, §9.2). Owner: M07.
// The only data source is packages/contracts/env/presets.json embedded by the contract generator (@awr/contracts/presets
// PRESETS, PRESETS_SHA256). EnvScalars are 21 float64 in fields[] order; the 4 user axes (wind direction, vertical wind,
// ISA offset, humidity) are never part of a preset snapshot. When a keyframe carries another presets_sha256 the store
// fetches GET /api/env/presets and builds a model from the server document (C08); every function takes the model.
import { NF, PRESETS, PRESETS_SHA256 } from '@awr/contracts/presets'

export const F = {
  SPEED_REF: 0, DIR: 1, W_MEAN: 2, SIGMA_REF: 3, GUST_AMP: 4, GUST_RATE: 5, GUST_LEN: 6, COVER: 7, CTYPE: 8, BASE: 9, TOP: 10,
  RAIN: 11, SNOW: 12, MOR_BG: 13, FOG_TOP: 14, DUST: 15, ISA_DT: 16, RH: 17, LIGHTNING: 18, HORIZON: 19, CLOUD2D: 20,
} as const
export { NF }

/** interpolation space code: 0 lin, 1 log, 2 arc */
export type SpaceCode = 0 | 1 | 2
export type Group = 'wind' | 'cloud' | 'precip' | 'vis' | 'misc'
export interface ProfileCfg { kind: 'log' | 'power' | 'uniform'; z_ref_m: number; z0_m: number; d_m: number; alpha: number; adv_height_m: number }

// minimal shape of presets.json used here (the generated Presets type is a 21-tuple; keep this loose)
interface PresetsDoc {
  constants: Record<string, number | number[]>
  fields: { path: string; group: string; space: string; min: number; max: number; user_axis?: boolean }[]
  windows: Record<'enter' | 'leave', Record<string, number[]>>
  rates_per_s: Record<string, number>
  durations_s: Record<string, number>
  routes: { from: string; to: string; via: string[] }[]
  presets: { id: string; icon?: string; name?: string; name_zh?: string; scalars: Record<string, Record<string, number>> }[]
  defaults: { scalars: Record<string, Record<string, number>>; config: Record<string, unknown> }
  client?: Record<string, number | number[]>
}

function getPath(d: Record<string, Record<string, number>> | undefined, path: string): number | undefined {
  const [g, k] = path.split('.')
  const v = d?.[g]?.[k]
  return typeof v === 'number' ? v : undefined
}

export class PresetsModel {
  readonly sha256: string
  readonly nf: number
  readonly space: Uint8Array
  readonly rate: Float64Array
  /** per field [w0, w1] of the enter and leave windows (flat, 2 per field) */
  readonly winEnter: Float64Array
  readonly winLeave: Float64Array
  readonly lo: Float64Array
  readonly hi: Float64Array
  readonly userAxis: Uint8Array
  readonly ids: readonly string[]
  readonly icons: Readonly<Record<string, string>>
  readonly defaults: Float64Array
  readonly defaultProfile: ProfileCfg
  readonly defaultConfig: Record<string, unknown>
  readonly c: Readonly<Record<string, number>>
  readonly precipGate: readonly [number, number]
  readonly client: Readonly<Record<string, number | number[]>>
  private readonly snaps = new Map<string, Float64Array>()
  private readonly routes = new Map<string, readonly string[]>()

  constructor(doc: PresetsDoc, sha256: string) {
    this.sha256 = sha256
    const n = doc.fields.length
    this.nf = n
    this.space = new Uint8Array(n)
    this.rate = new Float64Array(n)
    this.winEnter = new Float64Array(2 * n)
    this.winLeave = new Float64Array(2 * n)
    this.lo = new Float64Array(n)
    this.hi = new Float64Array(n)
    this.userAxis = new Uint8Array(n)
    this.defaults = new Float64Array(n)
    doc.fields.forEach((f, i) => {
      this.space[i] = f.space === 'log' ? 1 : f.space === 'arc' ? 2 : 0
      this.rate[i] = doc.rates_per_s[f.group]
      this.winEnter[2 * i] = doc.windows.enter[f.group][0]
      this.winEnter[2 * i + 1] = doc.windows.enter[f.group][1]
      this.winLeave[2 * i] = doc.windows.leave[f.group][0]
      this.winLeave[2 * i + 1] = doc.windows.leave[f.group][1]
      this.lo[i] = f.min
      this.hi[i] = f.max
      this.userAxis[i] = f.user_axis ? 1 : 0
      this.defaults[i] = getPath(doc.defaults.scalars, f.path) ?? 0
    })
    const icons: Record<string, string> = {}
    for (const p of doc.presets) {
      const v = new Float64Array(n).fill(Number.NaN)
      doc.fields.forEach((f, i) => {
        const x = getPath(p.scalars, f.path)
        if (x !== undefined) v[i] = x
      })
      this.snaps.set(p.id, v)
      icons[p.id] = p.icon ?? `env.${p.id}`
    }
    this.icons = icons
    this.ids = doc.presets.map((p) => p.id)
    for (const r of doc.routes) this.routes.set(`${r.from}>${r.to}`, r.via)
    const c: Record<string, number> = {}
    for (const [k, v] of Object.entries(doc.constants)) if (typeof v === 'number') c[k] = v
    this.c = c
    const g = doc.constants.precip_gate_cover as number[]
    this.precipGate = [g[0], g[1]]
    this.defaultConfig = doc.defaults.config
    this.defaultProfile = (doc.defaults.config as { wind: { profile: ProfileCfg } }).wind.profile
    this.client = doc.client ?? {}
  }

  snapshot(id: string): Float64Array | undefined {
    return this.snaps.get(id)
  }

  /** out = base overlaid with the preset snapshot (user axes kept) */
  overlay(base: ArrayLike<number>, id: string, out: Float64Array): Float64Array {
    const s = this.snaps.get(id)
    for (let i = 0; i < this.nf; i++) out[i] = s !== undefined && !Number.isNaN(s[i]) ? s[i] : base[i]
    return out
  }

  presetVector(id: string, base: ArrayLike<number> = this.defaults): Float64Array {
    return this.overlay(base, id, new Float64Array(this.nf))
  }

  route(from: string | null, to: string): readonly string[] {
    return from === null ? [] : this.routes.get(`${from}>${to}`) ?? []
  }

  /** the preset whose 17 fields equal s (relative 1e-9), else null */
  matchPreset(s: ArrayLike<number>): string | null {
    for (const id of this.ids) {
      const snap = this.snaps.get(id)!
      let ok = true
      for (let i = 0; i < this.nf && ok; i++) {
        const v = snap[i]
        if (!Number.isNaN(v) && Math.abs(s[i] - v) > 1e-9 * Math.max(1, Math.abs(v))) ok = false
      }
      if (ok) return id
    }
    return null
  }
}

/** the contract model (same bytes as the server when config.presets_sha256 matches) */
export const PRESETS_MODEL = new PresetsModel(PRESETS as unknown as PresetsDoc, PRESETS_SHA256)
export { PRESETS_SHA256 }

/** model from a server document (C08 hash mismatch) */
export function presetsFromJson(text: string, sha256: string): PresetsModel {
  return new PresetsModel(JSON.parse(text) as PresetsDoc, sha256)
}
