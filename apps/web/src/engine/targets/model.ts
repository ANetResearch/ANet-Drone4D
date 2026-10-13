// Target data on the page (M18-FR-018, FR-022, FR-024). Owner: M18. Plain state outside React (engine/ does not depend on
// stores): the latest TargetLite32 rows (10 Hz), the latest awr.target.detail.v1 per target (1-2 Hz) and the full
// configuration when a detail carried `cfg` (cfg_rev changed, every 10 s, or the target/add|update result). The viewport
// layer reads it every frame through `version`; stores/targets.ts mirrors a UI summary at the shared UI tick.
// Bodies come from the configuration, else from detail.sensitivity, else from the class template (until cfg arrives).
import { CLASS_DEFAULTS, CLASS_OF_CODE, type TargetClassName } from './classes'
import { decodeTargetLite32, newTargetRows, rowsBytes, type TargetRows } from './decode'
import { bodyVolumes, DEFAULT_MARGIN_M, isMoving, predictEntry, type SensitivityBody, type Volume } from './geometry'

export interface DetailBody {
  idx: number
  kind: SensitivityBody['kind']
  shape: SensitivityBody['shape']
  radius_cfg_m: number
  radius_eff_m?: Record<string, number>
  z_min_m?: number
  z_max_m?: number
  half_angle_deg?: number
  azimuth_offset_deg?: number
  enabled: boolean
  occupied_by?: string[]
}

export interface TargetDetail {
  id: string
  label?: string
  class: string
  t_sim_ns?: number
  awareness: string
  alertness: number
  criterion_mode: 'geometric' | 'physical'
  level_req?: 'D' | 'R' | 'I'
  discovered_count: number
  episode?: number
  last_discovered?: { t_sim_ns: number; uav: string; kind: string; range_m?: number; margin_db?: number | null; margin_ratio?: number | null } | null
  dwell?: { uav: string; kind?: string; t_s: number; t_dwell_s: number } | null
  behavior?: { phase?: 'onset' | 'response'; type?: string; t_left_s?: number | null } | null
  sensitivity: DetailBody[]
  inside_n?: number
  keepout?: { radius_m?: number; margin_m?: number; full_circle?: boolean }
  sound?: { on: boolean; phase_s?: number; lw_dba_now?: number } | null
  motion?: { mode?: string; speed_mps?: number; vz_mps?: number; path_idx?: number | null }
  capture?: { best_level: string; captured?: boolean; sensor?: string | null; uav?: string | null; n_eff?: number | null; n_req?: number | null } | null
  cfg_rev: number
  cfg?: TargetCfg
}

/** the awr.target.v1 object (only the fields the page reads are typed; the rest is kept as received) */
export interface TargetCfg {
  id: string
  label?: string
  class: string
  pos?: (number | null)[]
  size_m?: { l: number; w: number; h: number }
  motion?: { mode?: string; path?: (number | null)[][]; region?: number[][]; speed_mps?: number; [k: string]: unknown }
  sensitivity?: SensitivityBody[]
  sound?: Record<string, unknown> | null
  criterion_mode?: 'geometric' | 'physical'
  cfg_rev?: number
  [k: string]: unknown
}

export type BodyView = 'configured' | 'effective' | 'planning'

export interface IdResolver {
  targetIdOf?(targetNo: number): string | undefined
}

export class TargetsModel {
  readonly rows: TargetRows = newTargetRows()
  /** + 1 on every decoded rows message */
  version = 0
  /** + 1 on every detail or configuration change */
  detailVersion = 0
  tSimMs = 0
  readonly details = new Map<string, TargetDetail>()
  readonly cfgs = new Map<string, TargetCfg>()
  resolver: IdResolver | null = null

  idOfRow(i: number): string {
    const no = this.rows.agentNo[i]
    return this.resolver?.targetIdOf?.(no) ?? `target-${no}`
  }
  rowOf(id: string): number {
    for (let i = 0; i < this.rows.n; i++) if (this.idOfRow(i) === id) return i
    return -1
  }
  classOfRow(i: number): TargetClassName | null {
    return CLASS_OF_CODE[this.rows.cls[i]] ?? null
  }

  /** a DataMsg-like `{topic, data, tSimMs}`: returns true when it was a target topic */
  ingest(m: { topic: string; data: unknown; tSimMs?: number }): boolean {
    if (m.topic === 'swarm/target/state') {
      const b = rowsBytes(m.data)
      if (!b) return true
      this.ingestRows(b, m.tSimMs ?? this.tSimMs)
      return true
    }
    const mm = /^target\/([^/]+)\/detail$/.exec(m.topic)
    if (mm && m.data && typeof m.data === 'object') {
      this.ingestDetail(mm[1], m.data as TargetDetail)
      return true
    }
    return false
  }

  ingestRows(bytes: Uint8Array, tSimMs: number): void {
    decodeTargetLite32(new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), Math.floor(bytes.byteLength / 32), this.rows)
    this.tSimMs = tSimMs
    this.version++
    // targets gone from the rows lose their cached detail and configuration
    if (this.details.size > this.rows.n || this.cfgs.size > this.rows.n) {
      const live = new Set<string>()
      for (let i = 0; i < this.rows.n; i++) live.add(this.idOfRow(i))
      for (const id of [...this.details.keys()]) if (!live.has(id)) this.details.delete(id)
      for (const id of [...this.cfgs.keys()]) if (!live.has(id)) this.cfgs.delete(id)
    }
  }

  ingestDetail(id: string, d: TargetDetail): void {
    this.details.set(id, d)
    if (d.cfg) this.cfgs.set(id, d.cfg)
    this.detailVersion++
  }

  /** a configuration from the target/add or target/update result */
  setConfig(cfg: TargetCfg): void {
    this.cfgs.set(cfg.id, cfg)
    this.detailVersion++
  }

  remove(id: string): void {
    this.details.delete(id)
    this.cfgs.delete(id)
    this.detailVersion++
  }

  sizeOf(id: string, cls: TargetClassName | null): [number, number, number] {
    const s = this.cfgs.get(id)?.size_m ?? (cls ? CLASS_DEFAULTS[cls].size_m : undefined)
    return s ? [s.l, s.w, s.h] : [0.5, 0.5, 0.5]
  }

  bodiesOf(id: string, cls: TargetClassName | null): SensitivityBody[] {
    const c = this.cfgs.get(id)?.sensitivity
    if (c) return c
    const d = this.details.get(id)?.sensitivity
    if (d) {
      return d.map((b) => ({ kind: b.kind, shape: b.shape, radius_max_m: b.radius_cfg_m, z_min_m: b.z_min_m, z_max_m: b.z_max_m,
        half_angle_deg: b.half_angle_deg, azimuth_offset_deg: b.azimuth_offset_deg, enabled: b.enabled }))
    }
    return cls ? (CLASS_DEFAULTS[cls].sensitivity as readonly SensitivityBody[]).map((b) => ({ ...b })) : []
  }

  /** radii per body for the effective view (detail.radius_eff_m of the model; the configured radius when missing) */
  effectiveRadii(id: string, modelId: string | null): (number | null)[] | undefined {
    if (!modelId) return undefined
    const d = this.details.get(id)
    if (!d) return undefined
    const out: (number | null)[] = []
    for (const b of d.sensitivity) out[b.idx] = b.radius_eff_m?.[modelId] ?? null
    return out
  }

  /** world volumes of row i for a body view: configured, effective (per model) or planning (keepout: + margin, full circles) */
  volumesOfRow(i: number, view: BodyView, modelId: string | null = null): Volume[] {
    const id = this.idOfRow(i)
    const cls = this.classOfRow(i)
    const p: [number, number, number] = [this.rows.pos[3 * i], this.rows.pos[3 * i + 1], this.rows.pos[3 * i + 2]]
    const bodies = this.bodiesOf(id, cls)
    const moving = this.rows.moving[i] === 1 || isMoving(this.cfgs.get(id)?.motion?.mode, this.rows.speed[i])
    if (view === 'planning') return bodyVolumes(bodies, p, this.rows.heading[i], DEFAULT_MARGIN_M, moving, ['visual', 'acoustic'])
    const radii = view === 'effective' ? this.effectiveRadii(id, modelId) : undefined
    return bodyVolumes(bodies, p, this.rows.heading[i], 0, false, ['visual', 'acoustic'], radii)
  }

  /** manual flight warning (M18-FR-022, AWR-04 §10.6): the first target body the straight path enters within 3 s, against
   * the enabled configured bodies of the geometric targets and the effective bodies of the physical ones (moving: full
   * circles, as the server guard); null when none */
  manualWarning(p: readonly [number, number, number], v: readonly [number, number, number], modelId: string | null = null,
    horizonS = 3): { id: string; kind: string; volIdx: number; tS: number } | null {
    let best: { id: string; kind: string; volIdx: number; tS: number } | null = null
    for (let i = 0; i < this.rows.n; i++) {
      if (!this.rows.enabled[i]) continue
      const id = this.idOfRow(i)
      const cls = this.classOfRow(i)
      const pos: [number, number, number] = [this.rows.pos[3 * i], this.rows.pos[3 * i + 1], this.rows.pos[3 * i + 2]]
      const radii = this.rows.physical[i] ? this.effectiveRadii(id, modelId) : undefined
      const vols = bodyVolumes(this.bodiesOf(id, cls), pos, this.rows.heading[i], 0, this.rows.moving[i] === 1, ['visual', 'acoustic'], radii)
      for (const vol of vols) {
        const t = predictEntry(p, v, [vol], horizonS)
        if (t !== null && (best === null || t < best.tS)) best = { id, kind: vol.kind, volIdx: vol.volIdx, tS: t }
      }
    }
    return best
  }

  reset(): void {
    this.rows.n = 0
    this.details.clear()
    this.cfgs.clear()
    this.version++
    this.detailVersion++
  }
}

/** the page's target model (one per page; the rt client feeds it through stores/targets.ts) */
export const targetsModel = new TargetsModel()
