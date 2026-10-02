// Panel summary of the environment (M07-FR-034, FR-058, FR-061; M07 §8.1, §8.2). Owner: M07.
// Built from the EnvStore at the adapter's rate (Tier S <= 4 Hz, else <= 10 Hz) and written to stores/env.ts by
// viewport/layers/environment.tsx (engine/** never imports stores). The selected-vehicle readings come from the
// server's EnvSample32 raw record (uav/{id}/env; the drone runtime exposes each TelemetryFrame and EnvSampleCache keeps
// the newest record per vehicle, FX-WEB1) while it is fresh, else they are evaluated locally at the rendered pose with
// the same formulas (windCPU, sigma_at); airspeed = |v - W|. Allocates (a few Hz only).
import { sigmaAt } from './atmosphere/optics'
import { uvToFrom } from './state/conventions'
import type { EnvStore } from './state/EnvStore'
import { F } from './state/presets'
import { windCPU, type WindCpuOpts } from './wind/windCPU'
import { fAdv, profileCfg } from './wind/profile'
import type { TelemetryFrame } from '@/net/rt/types'

export interface EnvSelectedData { windMps: number; speedMps: number; dirFromDeg: number; gustMps: number; morM: number; rainEffMmh: number; airspeedMps: number; stale: boolean; source: 'sample32' | 'local' }

export interface EnvSummaryData {
  activePreset: string | null
  toPreset: string | null
  transition: { active: boolean; t0SimS: number; t1SimS: number; progress: number }
  scalars: { windSpeedRefMps: number; windDirFromDeg: number; morBgM: number; rainMmh: number; snowMmh: number; cloudCover: number }
  /** the keyframe targets (drafts may clear on these, M15) */
  target: { windSpeedRefMps: number; windDirFromDeg: number; morBgM: number; rainMmh: number; snowMmh: number; cloudCover: number }
  derived: { morM: number; rainEffMmh: number }
  beaufort: number
  selected: EnvSelectedData | null
  presetHashOk: boolean
  state: 'EMPTY' | 'SYNCED' | 'STALE' | 'EPOCH_WAIT'
  staleS: number
  version: number
  advanced: { turbSigmaRefMps: number; gustAmpMps: number; gustRateHz: number; wMeanMps: number; fogTopAglM: number; dust: number; level: number; turbModel: 'box' | 'dryden' | 'off' }
}

const BEAUFORT = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]
export function beaufort(v: number): number {
  if (!Number.isFinite(v)) return 0
  let b = 0
  while (b < BEAUFORT.length && v >= BEAUFORT[b]) b++
  return b
}

const w4 = new Float64Array(4)
const u3 = new Float64Array(3)
const sg = new Float64Array(2)

/** awr.EnvSample32.v1 byte offsets (packages/contracts/rt/layouts.json) and the raw item layout of the TelemetryFrame */
export const ES32 = { size: 32, wind: 0, sigmaExt: 20, rainEff: 24, flags: 28, gust: 30, rawSchema: 1, item: 80, payload: 16 } as const
/** records older than this (wall clock) fall back to the local evaluation */
export const ES32_MAX_AGE_MS = 1500

interface Es32Row { bytes: Uint8Array; dv: DataView; tSimMs: number; atMs: number }

/** newest EnvSample32 per vehicle, copied out of the TelemetryFrame raw items (the slot is reused after the next swap) */
export class EnvSampleCache {
  private readonly rows = new Map<number, Es32Row>()
  /** raw items of one TelemetryFrame; returns the number of EnvSample32 records taken */
  ingest(f: TelemetryFrame, nowMs: number): number {
    const r = f.raw
    const dv = r.bytes
    let n = 0
    for (let k = 0; k < r.count; k++) {
      const o = r.base + ES32.item * k
      if (dv.getUint8(o + 4) !== ES32.rawSchema || dv.getUint16(o + 6, true) < ES32.size) continue
      const a = dv.getUint16(o, true)
      let row = this.rows.get(a)
      if (!row) {
        const bytes = new Uint8Array(ES32.size)
        row = { bytes, dv: new DataView(bytes.buffer), tSimMs: 0, atMs: 0 }
        this.rows.set(a, row)
      }
      row.bytes.set(new Uint8Array(dv.buffer, dv.byteOffset + o + ES32.payload, ES32.size))
      row.tSimMs = dv.getFloat64(o + 8, true)
      row.atMs = nowMs
      n++
    }
    return n
  }
  /** the record of a vehicle when it is fresh and valid (flags bit 0), else null */
  get(agentNo: number, nowMs: number): DataView | null {
    const row = this.rows.get(agentNo)
    if (!row || nowMs - row.atMs > ES32_MAX_AGE_MS || (row.dv.getUint8(ES32.flags) & 1) === 0) return null
    return row.dv
  }
  clear(): void {
    this.rows.clear()
  }
}

/** readings from a server EnvSample32 record (wind ENU going-to, gust along the mean wind, extinction -> MOR) */
export function sampleReadings(dv: DataView, kMor: number, vel: ArrayLike<number> | null, stale: boolean): EnvSelectedData {
  const wx = dv.getFloat32(ES32.wind, true)
  const wy = dv.getFloat32(ES32.wind + 4, true)
  const wz = dv.getFloat32(ES32.wind + 8, true)
  uvToFrom(wx, wy, u3)
  const sigma = dv.getFloat32(ES32.sigmaExt, true)
  const air = vel ? Math.hypot(vel[0] - wx, vel[1] - wy, vel[2] - wz) : Number.NaN
  return { windMps: Math.hypot(wx, wy, wz), speedMps: u3[0], dirFromDeg: u3[1], gustMps: dv.getInt16(ES32.gust, true) * 0.01,
    morM: sigma > 0 ? kMor / sigma : Number.NaN, rainEffMmh: dv.getUint16(ES32.rainEff, true) * 0.01, airspeedMps: air, stale, source: 'sample32' }
}

/** readings at an ENU pose (local evaluation) */
export function localReadings(store: EnvStore, o: WindCpuOpts, pos: ArrayLike<number>, vel: ArrayLike<number> | null, stale: boolean, groundZ: number): EnvSelectedData {
  windCPU(pos[0], pos[1], pos[2], store, o, w4)
  uvToFrom(w4[0], w4[1], u3)
  const d = store.derived
  const s = store.scalars
  sigmaAt(pos[2] - groundZ, d, s[F.FOG_TOP], s[F.BASE], sg)
  const air = vel ? Math.hypot(vel[0] - w4[0], vel[1] - w4[1], vel[2] - w4[2]) : Number.NaN
  return { windMps: Math.hypot(w4[0], w4[1], w4[2]), speedMps: u3[0], dirFromDeg: u3[1], gustMps: w4[3], morM: d.mor_m > 0 ? store.P.c.k_mor / sg[0] : Number.NaN,
    rainEffMmh: d.rain_eff_mmh, airspeedMps: air, stale, source: 'local' }
}

export function buildSummary(store: EnvStore, nowMs: number, selected: EnvSelectedData | null): EnvSummaryData {
  const s = store.scalars
  const kf = store.current
  const to = kf?.to
  const tr = store.transition({ active: false, t0SimS: 0, t1SimS: 0, progress: 0 })
  const nan = Number.NaN
  const ok = !!kf
  return {
    activePreset: store.activePreset(),
    toPreset: kf?.toPreset ?? null,
    transition: tr,
    scalars: ok ? { windSpeedRefMps: s[F.SPEED_REF], windDirFromDeg: s[F.DIR], morBgM: s[F.MOR_BG], rainMmh: s[F.RAIN], snowMmh: s[F.SNOW], cloudCover: s[F.COVER] }
      : { windSpeedRefMps: nan, windDirFromDeg: nan, morBgM: nan, rainMmh: nan, snowMmh: nan, cloudCover: nan },
    target: to ? { windSpeedRefMps: to[F.SPEED_REF], windDirFromDeg: to[F.DIR], morBgM: to[F.MOR_BG], rainMmh: to[F.RAIN], snowMmh: to[F.SNOW], cloudCover: to[F.COVER] }
      : { windSpeedRefMps: nan, windDirFromDeg: nan, morBgM: nan, rainMmh: nan, snowMmh: nan, cloudCover: nan },
    derived: ok ? { morM: store.derived.mor_m, rainEffMmh: store.derived.rain_eff_mmh } : { morM: nan, rainEffMmh: nan },
    beaufort: ok ? beaufort(s[F.SPEED_REF]) : 0,
    selected,
    presetHashOk: !store.presetsMismatch,
    state: store.state,
    staleS: store.state === 'STALE' ? (nowMs - store.lastRecvMs) / 1000 : 0,
    version: store.version,
    advanced: ok ? { turbSigmaRefMps: s[F.SIGMA_REF], gustAmpMps: s[F.GUST_AMP], gustRateHz: s[F.GUST_RATE], wMeanMps: s[F.W_MEAN], fogTopAglM: s[F.FOG_TOP],
      dust: s[F.DUST], level: kf.level, turbModel: kf.turbModel }
      : { turbSigmaRefMps: nan, gustAmpMps: nan, gustRateHz: nan, wMeanMps: nan, fogTopAglM: nan, dust: nan, level: 1, turbModel: 'box' },
  }
}

/** wind profile card (M07 §8.2): 31 heights 0-150 m, mean s f(z) and the mean plus the strongest active front */
export function profileCurve(store: EnvStore): { t: number; v: number; env: number }[] {
  const kf = store.current
  if (!kf) return []
  const s = store.scalars
  let g = 0
  for (const ev of kf.events) if (ev.kind === 1 && fAdv(kf.profile) * store.anchors.sM - ev.x0 <= ev.sSpan + ev.lam) g = Math.max(g, ev.amp)
  const out = []
  for (let i = 0; i <= 30; i++) {
    const z = i * 5
    const v = s[F.SPEED_REF] * profileCfg(z, kf.profile)
    out.push({ t: z, v, env: v + g })
  }
  return out
}

/** 120 s ring of the selected vehicle wind, bucketed per second {t, min, mean, max, n} (M07 §8.2) */
export class EnvSeries {
  private readonly t = new Float64Array(1200)
  private readonly v = new Float32Array(1200)
  private n = 0
  private head = 0

  push(tS: number, w: number): void {
    this.t[this.head] = tS
    this.v[this.head] = w
    this.head = (this.head + 1) % this.t.length
    this.n = Math.min(this.n + 1, this.t.length)
  }

  clear(): void {
    this.n = 0
    this.head = 0
  }

  buckets(nowS: number, windowS = 120): { t: number; min: number; mean: number; max: number; n: number }[] {
    const b = new Map<number, { t: number; min: number; mean: number; max: number; n: number }>()
    for (let i = 0; i < this.n; i++) {
      const k = (this.head - 1 - i + this.t.length) % this.t.length
      const t = this.t[k]
      if (t < nowS - windowS) continue
      const key = Math.floor(t)
      const e = b.get(key) ?? { t: key, min: Number.POSITIVE_INFINITY, mean: 0, max: Number.NEGATIVE_INFINITY, n: 0 }
      const v = this.v[k]
      e.min = Math.min(e.min, v)
      e.max = Math.max(e.max, v)
      e.mean = (e.mean * e.n + v) / (e.n + 1)
      e.n++
      b.set(key, e)
    }
    return [...b.values()].sort((a, c) => a.t - c.t)
  }
}
