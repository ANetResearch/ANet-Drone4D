// Vehicle and sensor catalog store (M21; AWR-20 §9; AWR-04 §5.4; AWR-17 §17.6.2 SB-21 to SB-27; 14 §20.6.2 to §20.6.4).
// Owner: M21 (store), the Sheet, cards, parameter table and clone form are M15 JSX (14 §20.6). Data:
//   * public, cacheable catalog routes (viewer and operator, no token; D2-AC-37): GET /api/sandbox/v1/catalog/models,
//     /catalog/models/{id}, /catalog/sensors; the ETag of the last answer is kept and sent as If-None-Match, a 304
//     keeps the cached body;
//   * session clones (operator, sandbox token): GET/POST /sessions/{sid}/models, PATCH/DELETE .../models/{id}; a 422
//     with code 522 carries detail.violations[{field, rule, value, limit, message_zh}] that the clone form maps to fields
//     (fieldErrors), unmapped ones go to the form-level alert.
// Reference models (`reference = true`) always show DISCLAIMER_ZH (AWR-20 CAT-FR-004). Every text that comes from the
// server (names, summaries, violation messages) goes through lib/sanitize before it is stored. Instant GSD and
// recognition-distance hints call the M19 TS port (engine/perception) when it is delivered; gsdCm() below is the
// closed form GSD = R·p'/f (AWR-20 §6.2) used by the catalog cards.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { sanitizeText } from '@/lib/sanitize'
import { ApiError, apiDelete, apiGet, apiPatch, apiPost } from '@/net/api'
import { apiUrl } from '@/net/endpoint'

export const CATALOG_API = '/api/sandbox/v1/catalog'
export const DISCLAIMER_ZH = '模拟参考值，非任何真实产品'

export type VehicleCategory = 'multirotor' | 'fixed_wing' | 'vtol'
export type CategoryFilter = 'all' | VehicleCategory
export type NestKind = 'box' | 'launcher' | 'vtol_pad'
export type SensorKind = 'eo' | 'nir' | 'nir_illuminator' | 'lwir' | 'radar' | 'acoustic' | 'lidar'
export type Confidence = 'A' | 'B' | 'C' | 'D' | 'label'

export interface ModelSummary {
  model_id: string
  name_zh: string
  category: VehicleCategory
  reference: boolean
  version: string
  mass_kg?: number
  mtow_kg?: number
  endurance?: { hover_s?: number; cruise_s?: number; loiter_s?: number; mission_s?: number }
  speed?: { cruise_mps?: number; max_mps?: number; min_mps?: number; stall_mps?: number }
  link_range_m?: number
  default_sensors?: string[]
  optional_sensors?: string[]
  nest_kinds?: NestKind[]
  session_clone?: boolean
}

export interface ParamRow { group?: string; key: string; label_zh?: string; value: unknown; unit?: string | null; confidence: Confidence; use_zh?: string }
export interface SlotRow {
  slot: string; sensor_no: number; model_id: string | null; type: string | null; default: boolean; removable: boolean; legacy: boolean
  mass_kg: number | null; power_w: number | null; conf: string | null; on_gimbal_of: string | null
}
export interface CheckRow { id: string; value: unknown; range: unknown; pass: boolean; msg?: string }
export interface LoiterRadiusRow { w_max_mps: number; airspeed_mps: number; r_loiter_min_m: number; holdable: boolean }

export interface ModelDetail extends ModelSummary {
  profile?: Record<string, unknown>
  params_table?: ParamRow[]
  derived?: Record<string, number | null>
  frame_type?: string
  frame_kind?: number
  catalog?: { reference: boolean; disclaimer_zh: string | null; size_class?: string; order?: number; summary_zh?: string; limits_default?: string; visible?: boolean }
  limits?: Record<string, number>
  slots?: SlotRow[]
  signature?: { acoustic?: Record<string, unknown>; visual?: Record<string, unknown>; rcs_m2?: number | null; ir?: Record<string, unknown> }
  capabilities?: string[]
  checks?: CheckRow[]
  sensors?: Record<string, unknown>
  battery?: { pack: Record<string, unknown> | null; chargers: { model: string; compatible: boolean; charge_time_s: number | null }[] } | null
  fw?: { airframe?: Record<string, unknown>; derived?: Record<string, number>; loiter_radius_table?: LoiterRadiusRow[]; vtol?: Record<string, unknown> | null; mission?: Record<string, number> | null }
  base?: string
  set?: Record<string, number>
  default_mounts?: Record<string, boolean>
}

export interface SensorItem {
  sensor_type: string
  name_zh: string
  kind: SensorKind
  reference: boolean
  mass_kg?: number
  power_w?: number
  mount_requires_landed?: boolean
  spec: Record<string, unknown>
}

export interface Violation { field: string; rule: string; value?: unknown; limit?: unknown; message_zh?: string }
export type LoadState = 'idle' | 'loading' | 'ready' | 'error'

export interface CatalogState {
  state: LoadState
  error: string | null
  models: readonly ModelSummary[]
  details: ReadonlyMap<string, ModelDetail>
  sensors: readonly SensorItem[]
  /** session clones (SB-24), shown after the preset models with the "本会话" badge */
  sessionModels: readonly ModelSummary[]
  filter: CategoryFilter
  query: string
  selectedId: string | null
  /** last 522 violations of the clone form: field -> message (form fields) and the rest (form-level alert) */
  fieldErrors: Readonly<Record<string, string>>
  formErrors: readonly string[]
  saving: boolean
  version: number
}

const etags = new Map<string, string>()

export const catalogStore = createAwrStore<CatalogState>('catalog', () => ({
  state: 'idle', error: null, models: [], details: new Map(), sensors: [], sessionModels: [], filter: 'all', query: '',
  selectedId: null, fieldErrors: {}, formErrors: [], saving: false, version: 0,
}))

export function useCatalog<T>(selector: (s: CatalogState) => T): T {
  return useStore(catalogStore, selector)
}

// ------------------------------------------------------------------ helpers
const clean = (v: unknown, max = 200): string => (typeof v === 'string' ? sanitizeText(v, max) : '')

function cleanSummary(m: ModelSummary): ModelSummary {
  return { ...m, name_zh: clean(m.name_zh, 80) || m.model_id }
}

function cleanDetail(d: ModelDetail): ModelDetail {
  const out: ModelDetail = { ...cleanSummary(d) }
  if (d.catalog) out.catalog = { ...d.catalog, summary_zh: clean(d.catalog.summary_zh, 400), disclaimer_zh: d.catalog.reference ? DISCLAIMER_ZH : null }
  if (d.params_table) out.params_table = d.params_table.map((r) => ({ ...r, label_zh: clean(r.label_zh, 80), use_zh: clean(r.use_zh, 120) }))
  if (d.checks) out.checks = d.checks.map((c) => (c.msg ? { ...c, msg: clean(c.msg, 300) } : c))
  return out
}

/** public catalog GET with If-None-Match (no token): a 304 returns null and the caller keeps its cached body */
async function getCached<T>(path: string): Promise<T | null> {
  const headers: Record<string, string> = { accept: 'application/json' }
  const tag = etags.get(path)
  if (tag) headers['if-none-match'] = tag
  const r = await fetch(apiUrl(path), { headers })
  if (r.status === 304) return null
  if (!r.ok) {
    let p: { code?: number; message?: string; detail?: unknown } = {}
    try {
      p = (await r.json()) as typeof p
    } catch {
      p = {}
    }
    throw new ApiError(r.status, typeof p.code === 'number' ? p.code : null, p.message ?? `${r.status} ${path}`, null, null, p.detail ?? null)
  }
  const et = r.headers.get('etag')
  if (et) etags.set(path, et)
  return (await r.json()) as T
}

/** violations of a 522 reply -> field errors (form fields by path) and form-level messages */
export function mapViolations(v: readonly Violation[] | undefined, fields: readonly string[] = []): { fieldErrors: Record<string, string>; formErrors: string[] } {
  const fieldErrors: Record<string, string> = {}
  const formErrors: string[] = []
  for (const x of v ?? []) {
    const msg = clean(x.message_zh, 200) || `${x.rule} ${x.field}`
    const f = String(x.field ?? '')
    if (fields.length === 0 || fields.includes(f)) fieldErrors[f] = msg
    else formErrors.push(msg)
  }
  return { fieldErrors, formErrors }
}

// ------------------------------------------------------------------ selectors
export function categoryOf(m: ModelSummary): VehicleCategory {
  return m.category
}

export function isReference(m: Pick<ModelSummary, 'reference'>): boolean {
  return m.reference === true
}

/** cards in the Sheet: presets by catalog order, then session clones; category toggle and text search */
export function visibleModels(s: Pick<CatalogState, 'models' | 'sessionModels' | 'filter' | 'query'>): ModelSummary[] {
  const q = s.query.trim().toLowerCase()
  return [...s.models, ...s.sessionModels].filter((m) => (s.filter === 'all' || m.category === s.filter) &&
    (!q || m.model_id.includes(q) || m.name_zh.toLowerCase().includes(q)))
}

/** headline endurance for a card: hover for multirotors, cruise and loiter for fixed wing, mission for VTOL (seconds) */
export function headlineEndurance(m: ModelSummary): { kind: 'hover' | 'cruise' | 'mission'; s: number } | null {
  const e = m.endurance ?? {}
  if (m.category === 'multirotor' && e.hover_s !== undefined) return { kind: 'hover', s: e.hover_s }
  if (m.category === 'vtol' && e.mission_s !== undefined) return { kind: 'mission', s: e.mission_s }
  if (e.cruise_s !== undefined) return { kind: 'cruise', s: e.cruise_s }
  return null
}

/** ground sample distance (cm/px) of a camera at slant range R: GSD = R·p'/f with p' the effective pixel (µm) */
export function gsdCm(rangeM: number, focalMm: number, pixelUm: number): number {
  if (!(rangeM > 0 && focalMm > 0 && pixelUm > 0)) return Number.NaN
  return (rangeM * pixelUm * 1e-6) / (focalMm * 1e-3) * 100
}

/** mount mass margin of a configuration: MTOW minus model mass plus the change of the mounted payload (kg) */
export function mountMarginKg(d: Pick<ModelDetail, 'mass_kg' | 'mtow_kg' | 'slots'>, enabled: Readonly<Record<string, boolean>>): number {
  if (d.mass_kg === undefined || d.mtow_kg === undefined) return Number.NaN
  let dm = 0
  for (const s of d.slots ?? []) {
    if (s.legacy || s.mass_kg === null) continue
    const on = enabled[s.slot] ?? s.default
    if (on !== s.default) dm += on ? s.mass_kg : -s.mass_kg
  }
  return d.mtow_kg - (d.mass_kg + dm)
}

// ------------------------------------------------------------------ actions
function bump(patch: Partial<CatalogState>): void {
  catalogStore.setState({ ...patch, version: catalogStore.getState().version + 1 })
}

function errText(e: unknown): string {
  return e instanceof ApiError ? clean(e.message, 200) || `${e.status}` : clean(String(e), 200)
}

export const catalog = {
  /** SB-21 (and SB-23 when sensors = true); a 304 keeps the cached lists */
  async load(opts: { sensors?: boolean } = {}): Promise<void> {
    if (catalogStore.getState().state !== 'ready') bump({ state: 'loading', error: null })
    try {
      const m = await getCached<{ items: ModelSummary[] }>(`${CATALOG_API}/models`)
      const patch: Partial<CatalogState> = { state: 'ready', error: null }
      if (m) patch.models = (m.items ?? []).map(cleanSummary)
      if (opts.sensors) {
        const s = await getCached<{ items: SensorItem[] }>(`${CATALOG_API}/sensors`)
        if (s) patch.sensors = (s.items ?? []).map((x) => ({ ...x, name_zh: clean(x.name_zh, 80) || x.sensor_type }))
      }
      bump(patch)
    } catch (e) {
      bump({ state: 'error', error: errText(e) })
    }
  },

  /** SB-22 (presets) or the session clone detail from the clone replies */
  async detail(id: string): Promise<ModelDetail | null> {
    const have = catalogStore.getState().details.get(id)
    try {
      const d = await getCached<ModelDetail>(`${CATALOG_API}/models/${encodeURIComponent(id)}`)
      if (!d) return have ?? null
      const det = cleanDetail(d)
      const details = new Map(catalogStore.getState().details)
      details.set(id, det)
      bump({ details })
      return det
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null
      bump({ error: errText(e) })
      return have ?? null
    }
  },

  /** SB-24: the session's clones */
  async loadSession(sid: string): Promise<void> {
    try {
      const r = await apiGet<{ items: ModelSummary[] }>(`/api/sandbox/v1/sessions/${sid}/models`)
      bump({ sessionModels: (r.items ?? []).map((m) => ({ ...cleanSummary(m), session_clone: true })) })
    } catch (e) {
      bump({ error: errText(e) })
    }
  },

  /** SB-25: clone and edit; 522 violations are mapped to the form (fields = the form's field paths) */
  async createClone(sid: string, body: { from_model_id: string; label?: string; patch?: Record<string, unknown> },
    fields: readonly string[] = []): Promise<ModelDetail | null> {
    return saveClone(() => apiPost<ModelDetail>(`/api/sandbox/v1/sessions/${sid}/models`, body), fields)
  },

  /** SB-26 */
  async updateClone(sid: string, id: string, body: { label?: string; patch?: Record<string, unknown> },
    fields: readonly string[] = []): Promise<ModelDetail | null> {
    return saveClone(() => apiPatch<ModelDetail>(`/api/sandbox/v1/sessions/${sid}/models/${encodeURIComponent(id)}`, body), fields)
  },

  /** SB-27; 409 (105) when an instance still uses the clone */
  async deleteClone(sid: string, id: string): Promise<boolean> {
    try {
      await apiDelete(`/api/sandbox/v1/sessions/${sid}/models/${encodeURIComponent(id)}`)
      const details = new Map(catalogStore.getState().details)
      details.delete(id)
      bump({ details, sessionModels: catalogStore.getState().sessionModels.filter((m) => m.model_id !== id) })
      return true
    } catch (e) {
      bump({ formErrors: [errText(e)] })
      return false
    }
  },

  setFilter(filter: CategoryFilter): void {
    bump({ filter })
  },
  setQuery(query: string): void {
    bump({ query: sanitizeText(query, 40) })
  },
  select(selectedId: string | null): void {
    bump({ selectedId })
  },
  clearErrors(): void {
    bump({ fieldErrors: {}, formErrors: [] })
  },
  reset(): void {
    etags.clear()
    catalogStore.setState({ state: 'idle', error: null, models: [], details: new Map(), sensors: [], sessionModels: [], filter: 'all',
      query: '', selectedId: null, fieldErrors: {}, formErrors: [], saving: false, version: 0 })
  },
}

async function saveClone(send: () => Promise<ModelDetail>, fields: readonly string[]): Promise<ModelDetail | null> {
  bump({ saving: true, fieldErrors: {}, formErrors: [] })
  try {
    const d = cleanDetail({ ...(await send()), session_clone: true })
    const details = new Map(catalogStore.getState().details)
    details.set(d.model_id, d)
    const rest = catalogStore.getState().sessionModels.filter((m) => m.model_id !== d.model_id)
    const summary: ModelSummary = { model_id: d.model_id, name_zh: d.name_zh, category: d.category, reference: d.reference, version: d.version,
      mass_kg: d.mass_kg, mtow_kg: d.mtow_kg, endurance: d.endurance, speed: d.speed, link_range_m: d.link_range_m,
      default_sensors: d.default_sensors, optional_sensors: d.optional_sensors, nest_kinds: d.nest_kinds, session_clone: true }
    bump({ saving: false, details, sessionModels: [...rest, summary] })
    return d
  } catch (e) {
    if (e instanceof ApiError && e.reason === 522) {
      const det = (e.detail ?? {}) as { violations?: Violation[] }
      bump({ saving: false, ...mapViolations(det.violations, fields) })
    } else {
      bump({ saving: false, formErrors: [errText(e)] })
    }
    return null
  }
}
