// Panel summary of the environment (M07-FR-034, FR-058, FR-061; M07 §8.1, §8.2). Owner: M07.
// Built from the EnvStore at the adapter's rate (Tier S <= 4 Hz, else <= 10 Hz) and written to stores/env.ts by
// viewport/layers/environment.tsx (engine/** never imports stores). The selected-vehicle readings come from EnvSample32
// when the frame path exposes it, else they are evaluated locally at the rendered pose with the same formulas
// (windCPU, sigma_at); airspeed = |v - W|. Allocates (a few Hz only).
import { sigmaAt } from './atmosphere/optics'
import { uvToFrom } from './state/conventions'
import type { EnvStore } from './state/EnvStore'
import { F } from './state/presets'
import { windCPU, type WindCpuOpts } from './wind/windCPU'
import { fAdv, profileCfg } from './wind/profile'

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
