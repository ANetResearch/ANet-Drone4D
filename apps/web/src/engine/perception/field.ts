// Field view and perception volumes on the page (M19-FR-034, FR-036; AWR-04 §15.11, ADR-125; 17 §17.20.6). Owner: M19.
// All physics is computed by the server (python/awr/sim/perception/field.py); this module only decodes the three payloads,
// keeps the per-anchor state across delta frames and evaluates the tables at a point, for the field renderer (M06
// engine/field: deterministic pattern + instance values, at most 1 Hz) and the legend and readout card (M15):
//   field/{acoustic|visual|thermal|radar}  awr.field.profile.v1   radial profile v_r[32] on a shared log grid, elevation factor
//                                                                 f_el[6] (nearest cell), cut distance per elevation cell
//   field/volumes                           awr.field.volumes.v1   effective radius table 8 azimuths x 6 elevations per
//                                                                 (target, body, model) + transition width
//   target/{id}/quant                       awr.target.quant.v1    pair readings, columnar `soa1` on the topic
// Interpolation (the server's explicit samples SB-78 agree within 0.5 dB or 2 %, M19-NFR-009): radial linear in ln r for dB
// quantities (combine add) and in (ln r, ln v) for linear ones (combine mul); elevation: nearest cell of el_deg; e is the
// elevation of the vehicle seen from the observer (the sample point), i.e. -asin(u_z) for a point at direction u from the
// vehicle. Volumes: azimuth linear (circular) between the 45 deg cells, elevation nearest cell; P = 1/(1 + exp((r - r_eff)/(w/2.197)))
// with w scaled by r_eff(dir)/r_eff(ref) so the relative transition width is the same in every direction; P(r_eff) = 0.5.
// Framework-free (no React, no store); stores/perception.ts feeds it and the viewport reads `fieldModel.version`.
import { bodyVolumes, insideVolume, wrapPi, type BodyKind, type SensitivityBody, type Vec3 } from '../targets/geometry'

export type FieldKind = 'acoustic' | 'visual' | 'thermal' | 'radar'
export const FIELD_KINDS: readonly FieldKind[] = ['acoustic', 'visual', 'thermal', 'radar']
/** interest codes (bus interest streams.field) */
export const FIELD_CODE: Readonly<Record<FieldKind | 'volumes', number>> = { acoustic: 1, visual: 2, thermal: 3, radar: 4, volumes: 5 }
export const EL_DEG: readonly number[] = [-75, -45, -15, 15, 45, 75]
export const AZ_DEG: readonly number[] = [0, 45, 90, 135, 180, 225, 270, 315]
export const N_R = 32
const NONE_U16 = 0xffff
const LN9 = Math.log(9)
const V_SCALE = 10
const LOG_SCALE = 4000
const LOG_OFF = 8
const F_SCALE = 100
const DEG = Math.PI / 180

export interface ProfileEntry {
  id: string
  modelId: string
  rev: number
  /** field value at rM[k] (NaN: not drawn) */
  vR: Float64Array
  /** elevation factor per el cell (dB for add, factor for mul) */
  fEl: Float64Array
  /** cut distance per el cell (Infinity: no cut) */
  cutElM: Float64Array
  /** reference shell radius (m): default person (acoustic, visual) or D level (thermal, radar) */
  shellM: number
}

export interface ProfileFrame {
  tSimNs: number
  kind: FieldKind
  unit: 'dba' | 'arcmin' | 'px' | 'db'
  combine: 'add' | 'mul'
  rM: Float64Array
  elDeg: readonly number[]
  /** lower edges of bands 1..5 (bands[0] = nominal draw floor) */
  bands: readonly number[]
  /** dashed reference shells (field values): acoustic background L_bg, visual 1 arcmin, thermal 14 px */
  iso: readonly { id: string; v: number }[]
  shellRef: 'person' | 'D'
  ref: { sensor?: string; lens_mm?: number }
  /** anchors in roster order (the anchor index j of the pattern is the position in this list) */
  uavs: ProfileEntry[]
}

type Raw = Record<string, unknown>
const num = (v: unknown, d = Number.NaN): number => (typeof v === 'number' && Number.isFinite(v) ? v : d)
const str = (v: unknown, n = 64): string => (typeof v === 'string' ? v.slice(0, n) : '')
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

function decodeVr(q: unknown[], enc: string, vOff: number): Float64Array {
  const out = new Float64Array(N_R).fill(Number.NaN)
  for (let k = 0; k < Math.min(q.length, N_R); k++) {
    const x = num(q[k], NONE_U16)
    if (x === NONE_U16) continue
    out[k] = enc === 'log' ? 10 ** (x / LOG_SCALE - LOG_OFF) : x / V_SCALE + vOff
  }
  return out
}

function spread(q: unknown[], n: number, scale: number, zeroAs = 0): Float64Array {
  const out = new Float64Array(n)
  const v = q.length ? q : [0]
  for (let i = 0; i < n; i++) {
    const x = num(v.length === 1 ? v[0] : v[i], 0)
    out[i] = x === 0 && zeroAs !== 0 ? zeroAs : x / scale
  }
  return out
}

/**
 * decode one `awr.field.profile.v1` payload; `prev` (the previous frame of the same kind) supplies the grid, legend and the
 * arrays of vehicles whose rev did not change (delta frames carry only {id, rev}). Returns null when the payload is
 * malformed or a delta frame arrives before any keyframe of the same grid.
 */
export function decodeFieldProfile(data: unknown, prev: ProfileFrame | null = null): ProfileFrame | null {
  if (!data || typeof data !== 'object') return null
  const m = data as Raw
  const kind = m.kind as FieldKind
  if (!FIELD_KINDS.includes(kind)) return null
  const key = m.keyframe !== false && Array.isArray(m.r_m)
  const base = key ? null : prev && prev.kind === kind ? prev : null
  if (!key && !base) return null
  const rM = key ? Float64Array.from(arr(m.r_m).map((x) => num(x))) : base!.rM
  if (rM.length !== N_R) return null
  const enc = str(m.v_enc, 4) || 'lin'
  const vOff = num(m.v_off, 0)
  const items = arr(m.uav)
  const old = new Map((prev && prev.kind === kind ? prev.uavs : []).map((u) => [u.id, u]))
  const uavs: ProfileEntry[] = []
  for (const it0 of items) {
    const it = (it0 ?? {}) as Raw
    const id = str(it.id)
    if (!id) continue
    const rev = num(it.rev, 0)
    const src = typeof it.same === 'number' ? (items[it.same] as Raw | undefined) : it
    if (src && Array.isArray(src.v_r)) {
      uavs.push({ id, modelId: str(it.model_id ?? src.model_id ?? old.get(id)?.modelId, 40), rev, vR: decodeVr(arr(src.v_r), enc, vOff),
        fEl: spread(arr(src.f_el), EL_DEG.length, F_SCALE), cutElM: spread(arr(src.cut_el_m), EL_DEG.length, 1, Number.POSITIVE_INFINITY),
        shellM: num(src.shell_m, Number.NaN) })
      continue
    }
    const o = old.get(id)
    if (o && o.rev === rev) uavs.push(o)
  }
  return {
    tSimNs: num(m.t_sim_ns, 0), kind, unit: (str(m.unit, 8) || base?.unit || 'dba') as ProfileFrame['unit'],
    combine: (str(m.combine, 4) || base?.combine || 'add') as ProfileFrame['combine'], rM,
    elDeg: key ? arr(m.el_deg).map((x) => num(x)) : base!.elDeg, bands: key ? arr(m.bands).map((x) => num(x)) : base!.bands,
    iso: key ? arr(m.iso).map((x) => ({ id: str((x as Raw).id, 8), v: num((x as Raw).v) })) : base!.iso,
    shellRef: (key ? str(m.shell_ref, 8) : base!.shellRef) === 'D' ? 'D' : 'person', ref: key ? ((m.ref ?? {}) as ProfileFrame['ref']) : base!.ref, uavs,
  }
}

/** nearest elevation cell */
export function elCell(eDeg: number, el: readonly number[] = EL_DEG): number {
  let best = 0
  for (let j = 1; j < el.length; j++) if (Math.abs(el[j] - eDeg) < Math.abs(el[best] - eDeg)) best = j
  return best
}

/** field value of one anchor at slant range rM and observer elevation eDeg (NaN: not drawn) */
export function profileValue(f: ProfileFrame, u: ProfileEntry, rM: number, eDeg: number): number {
  const r = f.rM
  const x = Math.log(rM)
  const l0 = Math.log(r[0]), l1 = Math.log(r[N_R - 1])
  if (!(x >= l0 && x <= l1)) return Number.NaN
  const step = (l1 - l0) / (N_R - 1)
  const k = Math.min(N_R - 2, Math.max(0, Math.floor((x - l0) / step)))
  const lk = Math.log(r[k]), lk1 = Math.log(r[k + 1])
  const t = (x - lk) / (lk1 - lk)
  const a = u.vR[k], b = u.vR[k + 1]
  if (Number.isNaN(a) || Number.isNaN(b)) return Number.NaN
  const j = elCell(eDeg, f.elDeg)
  if (rM > u.cutElM[j]) return Number.NaN
  if (f.combine === 'add') return a + t * (b - a) + u.fEl[j]
  return Math.exp(Math.log(a) + t * (Math.log(b) - Math.log(a))) * u.fEl[j]
}

/** band index 1..5 of a field value (0: below the nominal floor or not drawn) */
export function profileBand(f: Pick<ProfileFrame, 'bands'>, v: number): number {
  if (Number.isNaN(v)) return 0
  let b = 0
  for (let i = 0; i < f.bands.length; i++) if (v >= f.bands[i]) b = i + 1
  return b
}

/** radius where the field equals `value` at elevation eDeg (monotone profiles; NaN if never reached) */
export function isoRadius(f: ProfileFrame, u: ProfileEntry, value: number, eDeg = 15): number {
  const j = elCell(eDeg, f.elDeg)
  const target = f.combine === 'add' ? value - u.fEl[j] : value / u.fEl[j]
  for (let k = 0; k < N_R - 1; k++) {
    const a = u.vR[k], b = u.vR[k + 1]
    if (Number.isNaN(a) || Number.isNaN(b)) continue
    if (a >= target && target >= b && a !== b) {
      const t = f.combine === 'add' ? (a - target) / (a - b) : (Math.log(a) - Math.log(target)) / (Math.log(a) - Math.log(b))
      const r = Math.exp(Math.log(f.rM[k]) + t * (Math.log(f.rM[k + 1]) - Math.log(f.rM[k])))
      return r > u.cutElM[j] ? Number.NaN : r
    }
  }
  return Number.NaN
}

// ------------------------------------------------------------------------------------------------ volumes
export interface VolumeBody {
  idx: number
  kind: BodyKind
  shape: SensitivityBody['shape']
  mode: 'geometric' | 'physical'
  /** per model (order of VolumesFrame.models): r_eff [8 az][6 el] in m, flattened az-major */
  rEffM: Float64Array[]
  wM: number[]
}

export interface VolumeTarget { id: string; rev: number; bodies: VolumeBody[] }

export interface VolumesFrame {
  tSimNs: number
  models: string[]
  targets: Map<string, VolumeTarget>
}

function expandTable(q: unknown[]): Float64Array {
  const nA = AZ_DEG.length, nE = EL_DEG.length
  const out = new Float64Array(nA * nE)
  const v = q.map((x) => num(x, 0) / 10)
  for (let i = 0; i < nA; i++) {
    for (let j = 0; j < nE; j++) {
      out[i * nE + j] = v.length === 1 ? v[0] : v.length === nE ? v[j] : v.length === nA ? v[i] : (v[i * nE + j] ?? 0)
    }
  }
  return out
}

/** decode `awr.field.volumes.v1`; non-keyframes update only the listed targets of `prev` */
export function decodeFieldVolumes(data: unknown, prev: VolumesFrame | null = null): VolumesFrame | null {
  if (!data || typeof data !== 'object') return null
  const m = data as Raw
  const key = m.keyframe === true
  if (!key && !prev) return null
  const models = arr(m.models).map((x) => str(x, 40))
  if (!key && prev && models.join(',') !== prev.models.join(',')) return null
  const targets = key ? new Map<string, VolumeTarget>() : new Map(prev!.targets)
  for (const t0 of arr(m.tgt)) {
    const t = (t0 ?? {}) as Raw
    const id = str(t.id)
    if (!id) continue
    const bodies: VolumeBody[] = arr(t.bodies).map((b0) => {
      const b = (b0 ?? {}) as Raw
      return { idx: num(b.idx, 0), kind: str(b.kind, 12) as BodyKind, shape: str(b.shape, 12) as SensitivityBody['shape'],
        mode: str(b.mode, 12) === 'physical' ? 'physical' : 'geometric', rEffM: arr(b.r_eff_dm).map((q) => expandTable(arr(q))),
        wM: arr(b.w_m).map((x) => num(x, 0)) }
    })
    targets.set(id, { id, rev: num(t.rev, 0), bodies })
  }
  return { tSimNs: num(m.t_sim_ns, 0), models, targets }
}

/** r_eff (m) of a table in direction (azimuth relative to the target heading, elevation from the target); azimuth linear, elevation nearest */
export function volumeREff(table: Float64Array, azRelRad: number, elRad: number): number {
  const nE = EL_DEG.length
  const a = ((((azRelRad / DEG) % 360) + 360) % 360) / 45
  const i0 = Math.floor(a) % 8, i1 = (i0 + 1) % 8
  const t = a - Math.floor(a)
  const j = elCell(elRad / DEG)
  return table[i0 * nE + j] * (1 - t) + table[i1 * nE + j] * t
}

/** display probability of the table model; w is scaled with r_eff(dir)/r_eff(ref) (same relative width in every direction) */
export function volumeP(rM: number, rEff: number, wM: number, rEffRef = rEff): number {
  if (!(rEff > 0)) return 0
  const w = Math.max(rEffRef > 0 ? (wM * rEff) / rEffRef : wM, 1e-6)
  const z = Math.min(700, Math.max(-700, ((rM - rEff) * LN9) / w))
  return 1 / (1 + Math.exp(z))
}

/** reference r_eff of a table (the direction where w_m was taken): azimuth 0 relative to the body offset is not on the grid,
 * so use the largest value (acoustic: the front; visual: the 15 deg side cell is the max of side cells) */
export function tableRef(table: Float64Array): number {
  let mx = 0
  for (const v of table) mx = Math.max(mx, v)
  return mx
}

/**
 * configured-body mask of field points (M18-FR-032: same implementation as the keepout prediction, engine/targets/geometry
 * insideVolume): `pts` is xyz interleaved (ENU m); writes 1/0 per point into `out` and returns the number inside.
 */
export function bodyMask(body: SensitivityBody, targetPos: Vec3, targetYawRad: number, pts: ArrayLike<number>, out: Uint8Array): number {
  const v = bodyVolumes([body], targetPos, targetYawRad, 0, false, [body.kind])[0]
  let n = 0
  const cnt = Math.floor(pts.length / 3)
  for (let i = 0; i < cnt; i++) {
    const ok = v ? insideVolume(v, pts[3 * i], pts[3 * i + 1], pts[3 * i + 2]) : false
    out[i] = ok ? 1 : 0
    if (ok) n++
  }
  return n
}

// ------------------------------------------------------------------------------------------------ pair readings
/** fixed scale of the columnar topic form (integers = round(value * scale)); same table as quant.py QUANT_SCALE */
export const QUANT_SCALE: Readonly<Record<string, number>> = {
  range_m: 10, elev_deg: 10, az_rel_deg: 10, los_age_s: 10, p_any: 1000, dwell_s: 10, t_dwell_s: 100,
  'acoustic.lp_dba': 10, 'acoustic.lp_band_db': 10, 'acoustic.band_hz': 1, 'acoustic.thr_dba': 10, 'acoustic.l_bg_dba': 10, 'acoustic.margin_db': 10,
  'acoustic.p': 1000,
  'visual.theta_arcmin': 100, 'visual.d_vis_m': 1000, 'visual.n_eq_px': 100, 'visual.theta_th_arcmin': 100, 'visual.ratio': 1000, 'visual.c_app': 1000,
  'visual.p': 1000,
  'radar.snr_db': 10, 'radar.snr_req_db': 10, 'radar.margin_db': 10, 'radar.p': 1000,
  'reverse.n_eff': 10, 'reverse.n_req': 10, 'reverse.p': 1000,
}
const SUB_FIELDS = {
  acoustic: ['lp_dba', 'lp_band_db', 'band_hz', 'thr_dba', 'l_bg_dba', 'margin_db', 'p', 'in_body'],
  visual: ['theta_arcmin', 'd_vis_m', 'n_eq_px', 'theta_th_arcmin', 'ratio', 'c_app', 'light_band', 'lights_on', 'in_fov', 'p', 'in_body'],
  radar: ['snr_db', 'snr_req_db', 'margin_db', 'p', 'in_body'],
  reverse: ['sensor', 'level', 'n_eff', 'n_req', 'p', 'limiting'],
} as const
const TOP = ['uav', 'model_id', 'range_m', 'elev_deg', 'az_rel_deg', 'los', 'los_age_s'] as const
const TAIL = ['p_any', 'would_discover', 'dwell_s', 't_dwell_s'] as const

export interface AcousticReading { lp_dba: number; lp_band_db: number; band_hz: number; thr_dba: number; l_bg_dba: number; margin_db: number; p: number; in_body: boolean }
export interface VisualReading {
  theta_arcmin: number; d_vis_m: number; n_eq_px: number | null; theta_th_arcmin: number; ratio: number; c_app: number; light_band: 'day' | 'low' | 'night'
  lights_on: boolean; in_fov: boolean; p: number; in_body: boolean
}
export interface RadarReading { snr_db: number; snr_req_db: number; margin_db: number; p: number; in_body: boolean }
export interface ReverseReading { sensor: string; level: 'none' | 'D' | 'R' | 'I'; n_eff: number | null; n_req: number | null; p: number | null; limiting: string | null }

/** one pair of awr.target.quant.v1 (expanded form, as SB-73 returns it) */
export interface PairReading {
  uav: string
  model_id: string
  range_m: number
  elev_deg: number
  az_rel_deg: number
  los: boolean | null
  los_age_s: number | null
  acoustic: AcousticReading | null
  visual: VisualReading | null
  radar?: RadarReading | null
  reverse: ReverseReading | null
  p_any: number
  would_discover: boolean
  dwell_s: number
  t_dwell_s: number
}

export interface QuantFrame {
  t_sim_ns: number
  target: string
  criterion_mode: 'geometric' | 'physical'
  pairs: PairReading[]
}

const at = (col: unknown, i: number): unknown => (Array.isArray(col) ? col[i] : col)
function deq(v: unknown, key: string): unknown {
  if (v === null || v === undefined || typeof v === 'boolean' || typeof v === 'string') return v ?? null
  const s = QUANT_SCALE[key]
  return s === undefined ? v : s === 1 ? v : (v as number) / s
}

/** decode `awr.target.quant.v1`: the columnar topic form (enc soa1) or the expanded REST form; strings are length-limited */
export function decodeTargetQuant(data: unknown): QuantFrame | null {
  if (!data || typeof data !== 'object') return null
  const m = data as Raw
  if (m.enc !== 'soa1') {
    if (!Array.isArray(m.pairs)) return null
    return { t_sim_ns: num(m.t_sim_ns, 0), target: str(m.target), criterion_mode: m.criterion_mode === 'physical' ? 'physical' : 'geometric',
      pairs: m.pairs as PairReading[] }
  }
  const n = Math.min(12, Math.max(0, num(m.n, 0)))
  const pairs: PairReading[] = []
  for (let i = 0; i < n; i++) {
    const p: Raw = {}
    for (const k of TOP) {
      const v = at(m[k], i)
      p[k] = k === 'uav' || k === 'model_id' ? str(v, k === 'uav' ? 32 : 40) : deq(v, k)
    }
    for (const sub of ['acoustic', 'visual', 'radar', 'reverse'] as const) {
      if (!(sub in m)) continue
      const cols = m[sub] as Raw | null
      if (!cols) { p[sub] = null; continue }
      if (cols._has !== undefined && !at(cols._has, i)) { p[sub] = null; continue }
      const o: Raw = {}
      for (const f of SUB_FIELDS[sub]) {
        const v = at(cols[f], i)
        o[f] = typeof v === 'string' ? str(v, 24) : deq(v, `${sub}.${f}`)
      }
      p[sub] = o
    }
    for (const k of TAIL) p[k] = deq(at(m[k], i), k)
    pairs.push(p as unknown as PairReading)
  }
  return { t_sim_ns: num(m.t_sim_ns, 0), target: str(m.target), criterion_mode: m.criterion_mode === 'physical' ? 'physical' : 'geometric', pairs }
}

// ------------------------------------------------------------------------------------------------ page model
/**
 * latest decoded field data (framework-free; stores/perception.ts writes, the field renderer reads by `version`):
 * one profile frame per subscribed kind, the volumes frame and the readings of the selected target.
 */
export const fieldModel = {
  profiles: new Map<FieldKind, ProfileFrame>(),
  volumes: null as VolumesFrame | null,
  quant: new Map<string, QuantFrame>(),
  version: 0,
  /** current field view and volume display (mirrored from the store for the renderer) */
  kind: 'none' as FieldKind | 'none',
  volumesMode: 'off' as 'off' | 'selected' | 'all',
  physicalRef: false,
  selectedTarget: null as string | null,
  reset(): void {
    this.profiles.clear()
    this.volumes = null
    this.quant.clear()
    this.version++
  },
}

/** anchor index j of a target body in the deterministic pattern (same rule as the server's SB-78 default) */
export const volumeAnchorIndex = (targetOrder: number, bodyIdx: number): number => targetOrder * 4 + bodyIdx

/** azimuth of a point relative to a target heading (rad, -pi..pi) */
export const azRel = (dx: number, dy: number, yawRad: number): number => wrapPi(Math.atan2(dy, dx) - yawRad)
