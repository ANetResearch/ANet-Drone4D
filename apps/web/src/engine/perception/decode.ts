// Perception data products on the wire (M19-FR-017, FR-026; AWR-04 §11.7; 17 §17.7.4; contracts sensor/perception,
// radar_tracks, acoustic_bearings, detection). Owner: M19. The rt client delivers msgpack payloads already decoded; these
// helpers check the shape, sanitise the strings (ids, sensor names) and give the HUD line of a frame: "9.6 / 14 px" and the
// limiting factor (i18n key perception.limiting.<name>; Chinese fallback LIMITING_ZH).
import { LIMITING, type LevelName, type Limiting } from './levels'

export interface PerceptionTarget {
  target: string
  cls_est?: string
  level: LevelName
  p?: number
  n?: number
  n_eff: number
  n_req: number
  level_req?: 'D' | 'R' | 'I'
  range_m: number
  gsd_m: number
  limiting: Limiting | null
  k?: { los?: number; c?: number; light?: number; blur?: number; occ?: number }
  hold_s?: number
  captured: boolean
  capture_state?: 'IDLE' | 'ARMING' | 'CAPTURED' | 'HOLDING'
  delta_t_app_k?: number
  meas_enu_m?: [number, number, number]
  sigma_m?: number
}

export interface PerceptionSensorFrame {
  sensor: string
  kind: 'eo' | 'nir' | 'lwir'
  mode?: string | null
  f_mm?: number
  hfov_rad?: number
  vfov_rad?: number
  illum?: { builtin?: string; ext?: string }
  n_in_fov?: number
  targets: PerceptionTarget[]
}

export interface PerceptionFrame {
  uav: string
  t_sim_ns: number
  sensors: PerceptionSensorFrame[]
  hud: { sensor: string; target: string; n_eff: number; n_req: number; limiting: Limiting | null } | null
  budget?: { rays_tick_max?: number; rays_s?: number; dropped?: number }
}

export interface HudInfo {
  sensor: string
  target: string
  nEff: number
  nReq: number
  /** "9.6 / 14 px" */
  text: string
  limiting: Limiting | null
  /** fraction of the required pixels reached (0-1), for a progress bar */
  progress: number
}

export const LIMITING_ZH: Readonly<Record<Limiting, string>> = { fov: '视锥', los: '视线', pixels: '像素', light: '照度', contrast: '对比度', blur: '模糊' }
const LEVELS = new Set(['none', 'D', 'R', 'I'])
const LIMS = new Set<string>(LIMITING)
const SAFE = /^[A-Za-z0-9_.:-]{1,48}$/

const str = (v: unknown): string => (typeof v === 'string' && SAFE.test(v) ? v : '')
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
const lim = (v: unknown): Limiting | null => (typeof v === 'string' && LIMS.has(v) ? (v as Limiting) : null)

function target(o: Record<string, unknown>): PerceptionTarget | null {
  const id = str(o.target)
  if (!id) return null
  const out: PerceptionTarget = {
    target: id, level: LEVELS.has(String(o.level)) ? (o.level as LevelName) : 'none', n_eff: num(o.n_eff), n_req: num(o.n_req), range_m: num(o.range_m),
    gsd_m: num(o.gsd_m), limiting: lim(o.limiting), captured: o.captured === true,
  }
  if (typeof o.cls_est === 'string') out.cls_est = str(o.cls_est) || 'unknown'
  if (typeof o.p === 'number') out.p = num(o.p)
  if (typeof o.n === 'number') out.n = num(o.n)
  if (o.level_req === 'D' || o.level_req === 'R' || o.level_req === 'I') out.level_req = o.level_req
  if (o.k && typeof o.k === 'object') out.k = o.k as PerceptionTarget['k']
  if (typeof o.hold_s === 'number') out.hold_s = num(o.hold_s)
  if (typeof o.capture_state === 'string' && ['IDLE', 'ARMING', 'CAPTURED', 'HOLDING'].includes(o.capture_state)) {
    out.capture_state = o.capture_state as PerceptionTarget['capture_state']
  }
  if (typeof o.delta_t_app_k === 'number') out.delta_t_app_k = num(o.delta_t_app_k)
  if (Array.isArray(o.meas_enu_m) && o.meas_enu_m.length === 3) out.meas_enu_m = [num(o.meas_enu_m[0]), num(o.meas_enu_m[1]), num(o.meas_enu_m[2])]
  if (typeof o.sigma_m === 'number') out.sigma_m = num(o.sigma_m)
  return out
}

/** a `uav/{id}/perception` payload as a typed frame; null when the shape is wrong. The rt client hands over decoded msgpack
 *  objects; raw bytes need the decoder of net/** (`unpack`, msgpack stays in net/, AWR-11 §3.4). */
export function decodePerception(buf: unknown, unpack?: (b: Uint8Array) => unknown): PerceptionFrame | null {
  let o: unknown = buf
  if (buf instanceof Uint8Array) {
    if (!unpack) return null
    try {
      o = unpack(buf)
    } catch {
      return null
    }
  }
  if (!o || typeof o !== 'object') return null
  const d = o as Record<string, unknown>
  const uav = str(d.uav)
  if (!uav || !Array.isArray(d.sensors)) return null
  const sensors: PerceptionSensorFrame[] = []
  for (const s of d.sensors as Record<string, unknown>[]) {
    if (!s || typeof s !== 'object') continue
    const kind = s.kind === 'eo' || s.kind === 'nir' || s.kind === 'lwir' ? s.kind : null
    const name = str(s.sensor)
    if (!kind || !name) continue
    const targets = Array.isArray(s.targets) ? (s.targets as Record<string, unknown>[]).map(target).filter((x): x is PerceptionTarget => x !== null) : []
    sensors.push({ sensor: name, kind, mode: typeof s.mode === 'string' ? s.mode : null, f_mm: num(s.f_mm), hfov_rad: num(s.hfov_rad), vfov_rad: num(s.vfov_rad),
      illum: s.illum && typeof s.illum === 'object' ? (s.illum as PerceptionSensorFrame['illum']) : undefined, n_in_fov: num(s.n_in_fov), targets })
  }
  const h = d.hud as Record<string, unknown> | null | undefined
  const hud = h && typeof h === 'object' && str(h.target) ? { sensor: str(h.sensor), target: str(h.target), n_eff: num(h.n_eff), n_req: num(h.n_req), limiting: lim(h.limiting) } : null
  return { uav, t_sim_ns: num(d.t_sim_ns), sensors, hud, budget: d.budget && typeof d.budget === 'object' ? (d.budget as PerceptionFrame['budget']) : undefined }
}

const fmt1 = (x: number): string => (Math.round(x * 10) / 10).toString()

/** HUD line of the nearest target in the frustum of `sensor` (default: the frame's HUD sensor); null hides the HUD */
export function hudLine(f: PerceptionFrame | null, sensor?: string): HudInfo | null {
  if (!f) return null
  let s = sensor ? f.sensors.find((x) => x.sensor === sensor) : undefined
  if (!s && f.hud) s = f.sensors.find((x) => x.sensor === f.hud?.sensor)
  if (!s) s = f.sensors.find((x) => x.targets.length > 0)
  const t = s?.targets[0]
  if (!s || !t) return null
  return { sensor: s.sensor, target: t.target, nEff: t.n_eff, nReq: t.n_req, text: `${fmt1(t.n_eff)} / ${fmt1(t.n_req)} px`, limiting: t.limiting,
    progress: t.n_req > 0 ? Math.min(1, Math.max(0, t.n_eff / t.n_req)) : 0 }
}

/** targets in CAPTURED (or HOLDING) state in a frame: FPV capture box candidates */
export function capturedTargets(f: PerceptionFrame | null): { sensor: string; target: string }[] {
  const out: { sensor: string; target: string }[] = []
  for (const s of f?.sensors ?? []) for (const t of s.targets) if (t.captured) out.push({ sensor: s.sensor, target: t.target })
  return out
}
