// Target panel model (R-D2-07 to R-D2-11, R-D2-18; AWR-04 §2.2, §7; M15-FR-212; UX-FR-119 to UX-FR-124; AWR-14 §20.7):
// pure helpers of the list filters, the front-end range checks of the property form (the server re-validates: 540 to
// 544), the keep-out volumes of the manual-control approach warning (configured radius plus the 20 m margin; a moving
// target's sector counts as a full circle) and the schematic waveform of a periodic sound source for the lieflat line
// chart (one period: the duty gate times a carrier of a few visible cycles, s(t) = sum a_k sin(2 pi k f t) gate(t)).
import type { Sensitivity, Sound, Target } from '@awr/contracts/types'
import type { KeepVolume } from '@/ui/manual/model'
import type { TargetItem } from '@/stores/targets'

export const TARGET_CLASSES = ['person', 'dog', 'vehicle', 'uav', 'machine', 'object'] as const
export type TargetClass = (typeof TARGET_CLASSES)[number]
export type Awareness = TargetItem['aware']
export type BestLevel = TargetItem['level']

export const KEEPOUT_MARGIN_M = 20
export const MAX_VOLUMES = 4
export const MAX_TARGETS = 30

export interface TargetRow {
  id: string
  label: string
  cls: TargetClass
  motion: 'static' | 'path' | 'random_walk'
  awareness: Awareness
  best: BestLevel
  captured: boolean
  discovered: number
}

export interface TargetFilter { cls: TargetClass | 'all'; awareness: Awareness | 'all'; best: BestLevel | 'all'; discoveredOnly: boolean }
export const NO_FILTER: TargetFilter = { cls: 'all', awareness: 'all', best: 'all', discoveredOnly: false }

export function filterRows(rows: readonly TargetRow[], f: TargetFilter): TargetRow[] {
  return rows.filter((r) => (f.cls === 'all' || r.cls === f.cls) && (f.awareness === 'all' || r.awareness === f.awareness)
    && (f.best === 'all' || r.best === f.best) && (!f.discoveredOnly || r.discovered > 0))
}

/** list rows from the targets store (M18: TargetLite32 rows and details); the motion mode comes from the configuration
 *  when known (`motionOf`), else from the row's moving flag */
export function buildRows(items: readonly Pick<TargetItem, 'id' | 'label' | 'cls' | 'aware' | 'level' | 'captured' | 'discovered' | 'moving'>[],
  motionOf: (id: string) => string | undefined = () => undefined): TargetRow[] {
  return items.map((x) => {
    const cls = ((TARGET_CLASSES as readonly string[]).includes(x.cls ?? '') ? x.cls : 'object') as TargetClass
    const mm = motionOf(x.id)
    const motion = mm === 'path' || mm === 'random_walk' ? mm : mm === 'static' ? 'static' : x.moving ? 'path' : 'static'
    return { id: x.id, label: x.label || x.id, cls, motion, awareness: x.aware, best: x.level, captured: x.captured, discovered: x.discovered }
  })
}

/** list items when the store has no TargetLite32 rows yet (the raw `swarm/target/state` channel is not delivered to the
 *  page client until M11's decode.ts passes it on, d2-WP-05-to-M11 item 1): the roster's target entries or the seeded
 *  configurations, with the live state of the `target/{id}/detail` messages (awareness, discovered count, best level) */
export function itemsFromDetails(entries: readonly { id: string; label?: string; cls?: string | null }[],
  details: ReadonlyMap<string, { label?: string; class?: string; awareness?: string; discovered_count?: number; capture?: { best_level?: string; captured?: boolean } | null; motion?: { mode?: string } }>,
  cfgs: ReadonlyMap<string, { id: string; label?: string; class?: string; motion?: { mode?: string } }>,
  keepCfg: (id: string) => boolean = () => true): Pick<TargetItem, 'id' | 'label' | 'cls' | 'aware' | 'level' | 'captured' | 'discovered' | 'moving'>[] {
  const ids = new Map<string, { label?: string; cls?: string | null }>()
  for (const e of entries) ids.set(e.id, { label: e.label, cls: e.cls })
  for (const [id, c] of cfgs) if (!ids.has(id) && keepCfg(id)) ids.set(id, { label: c.label, cls: c.class })
  const aware = new Set(['UNAWARE', 'SUSPICIOUS', 'REACTING', 'ALERTED', 'CALMING'])
  const lv = new Set(['none', 'D', 'R', 'I'])
  return [...ids.entries()].map(([id, e]) => {
    const d = details.get(id)
    const c = cfgs.get(id)
    const mode = c?.motion?.mode ?? d?.motion?.mode
    return {
      id, label: e.label || d?.label || c?.label || id, cls: ((e.cls ?? d?.class ?? c?.class ?? null) as TargetItem['cls']),
      aware: (aware.has(d?.awareness ?? '') ? d?.awareness : 'UNAWARE') as TargetItem['aware'],
      level: (lv.has(d?.capture?.best_level ?? '') ? d?.capture?.best_level : 'none') as TargetItem['level'],
      captured: d?.capture?.captured === true, discovered: d?.discovered_count ?? 0, moving: mode !== undefined && mode !== 'static',
    }
  })
}

/**
 * the roster is authoritative for the targets it has listed (AWR-17 §17.7.2 rule 4: targets share fleet/roster): a target
 * id seen in the roster that is no longer there was removed (by this page, another client or REST), and its cached
 * configuration must not keep the row alive (d2-WP-13-to-M15 item 1, D2-AC-11). Ids not yet seen in the roster (the
 * seeded REST list before the first roster, a target just added by this page) stay until the roster lists them.
 */
export class RosterTargetGate {
  private readonly seen = new Set<string>()
  private live = new Set<string>()
  /** the current roster target entries; returns the keep predicate of the configuration-only ids */
  update(entries: readonly { id: string }[]): (id: string) => boolean {
    this.live = new Set(entries.map((e) => e.id))
    for (const id of this.live) this.seen.add(id)
    return (id) => this.live.has(id) || !this.seen.has(id)
  }
  /** a new session or world: nothing seen yet */
  reset(): void {
    this.seen.clear()
    this.live.clear()
  }
}

// ------------------------------------------------------------ range checks (front-end; the server decides: 540-544)
export interface Range { min: number; max: number }
export const RANGES = {
  size: { min: 0.05, max: 30 },
  speed: { min: 0, max: 40 },
  contrast: { min: -1, max: 4 },
  unit: { min: 0, max: 1 },
  deltaT: { min: -20, max: 200 },
  emissivity: { min: 0.5, max: 1 },
  rcs: { min: 0.001, max: 1000 },
  lwDba: { min: 0, max: 140 },
  f0: { min: 20, max: 8000 },
  period: { min: 0.1, max: 600 },
  nReq: { min: 1, max: 200 },
  calm: { min: 0, max: 3600 },
  radius: { min: 1, max: 3000 },
  height: { min: 0, max: 3000 },
  halfAngle: { min: 1, max: 180 },
  azimuth: { min: -180, max: 180 },
} as const satisfies Record<string, Range>

export const inRange = (v: number, r: Range): boolean => Number.isFinite(v) && v >= r.min && v <= r.max

/** label rule: 1-32 characters after trimming */
export const labelOk = (s: string): boolean => {
  const n = Array.from(s.trim()).length
  return n >= 1 && n <= 32
}

/** field errors of a sensitivity list (empty when valid): index -> field keys */
export function sensitivityErrors(vols: readonly Sensitivity[]): Record<number, string[]> {
  const out: Record<number, string[]> = {}
  vols.forEach((v, i) => {
    const e: string[] = []
    if (!inRange(v.radius_max_m, RANGES.radius)) e.push('radius_max_m')
    const zMin = v.z_min_m ?? 0
    const zMax = v.z_max_m ?? v.radius_max_m
    if (!inRange(zMin, RANGES.height) || !inRange(zMax, RANGES.height) || zMax <= zMin) e.push('z')
    if (v.shape === 'sector' && !inRange(v.half_angle_deg ?? 60, RANGES.halfAngle)) e.push('half_angle_deg')
    if (e.length) out[i] = e
  })
  return out
}

export function soundErrors(s: Sound): string[] {
  const e: string[] = []
  if (!inRange(s.f0_hz, RANGES.f0)) e.push('f0_hz')
  if (!inRange(s.period_s, RANGES.period)) e.push('period_s')
  if (!inRange(s.duty, RANGES.unit)) e.push('duty')
  if (!inRange(s.lw_dba, RANGES.lwDba)) e.push('lw_dba')
  return e
}

/** a new volume of a kind with the class template's typical values */
export function newVolume(kind: 'visual' | 'acoustic'): Sensitivity {
  return kind === 'visual'
    ? { kind, shape: 'sector', radius_max_m: 300, z_min_m: 0, z_max_m: 300, half_angle_deg: 60, azimuth_offset_deg: 0, enabled: true }
    : { kind, shape: 'hemisphere', radius_max_m: 300, z_min_m: 0, z_max_m: 300, enabled: true }
}

// ------------------------------------------------------------ keep-out volumes (manual control warning)
export function keepVolumes(targets: readonly Target[]): KeepVolume[] {
  const out: KeepVolume[] = []
  for (const tg of targets) {
    if (!tg.id || tg.enabled === false || !tg.pos) continue
    const x = tg.pos[0]
    const y = tg.pos[1]
    if (x === null || y === null) continue
    const z = tg.pos[2] ?? 0
    const moving = (tg.motion?.mode ?? 'static') !== 'static'
    for (const v of tg.sensitivity ?? []) {
      if (v.enabled === false || (v.kind !== 'visual' && v.kind !== 'acoustic')) continue
      const shape = v.shape === 'sector' && moving ? 'cylinder' : v.shape
      out.push({ target: tg.id, kind: v.kind, shape, c: [x, y, z ?? 0], r: v.radius_max_m + KEEPOUT_MARGIN_M, zMin: v.z_min_m ?? 0, zMax: v.z_max_m ?? v.radius_max_m })
    }
  }
  return out
}

// ------------------------------------------------------------ schematic waveform (one period; P1, UX-FR-124)
const VISIBLE_CYCLES = 16

/** n samples of one period [0, period): {t: seconds, v: amplitude -1..1} (deterministic; noise uses a fixed LCG) */
export function waveform(s: Sound, n = 240): { t: number; v: number }[] {
  const period = Math.max(0.1, s.period_s)
  const duty = Math.min(1, Math.max(0, s.duty))
  const phase = s.phase_s ?? 0
  const harm = s.waveform === 'tone' ? [0] : s.harmonics_db?.length ? s.harmonics_db : s.waveform === 'harmonic' ? [0, -6, -10, -14] : [0]
  const amps = harm.map((db) => 10 ** (db / 20))
  const norm = amps.reduce((a, b) => a + b, 0) || 1
  let seed = 12345
  const out: { t: number; v: number }[] = []
  for (let i = 0; i < n; i++) {
    const t = (i / n) * period
    const local = ((t - phase) % period + period) % period
    const on = duty >= 1 || local < duty * period
    let v = 0
    if (on) {
      if (s.waveform === 'noise_band') {
        seed = (seed * 1103515245 + 12345) & 0x7fffffff
        v = (seed / 0x7fffffff) * 2 - 1
      } else {
        const x = (2 * Math.PI * VISIBLE_CYCLES * t) / period
        for (let k = 0; k < amps.length; k++) v += amps[k] * Math.sin((k + 1) * x)
        v /= norm
      }
    }
    out.push({ t, v })
  }
  return out
}
