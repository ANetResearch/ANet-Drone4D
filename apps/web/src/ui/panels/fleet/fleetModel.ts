// Fleet management model (R-D2-13, R-D2-15, R-D2-20; AWR-04 §5; AWR-20; M15-FR-211; UX-FR-115 to UX-FR-118; AWR-14
// §20.6): pure helpers of the model catalogue (category filter and search, the clone form's whitelisted fields with their
// units and front-end ranges, the derived quantities recomputed as the form changes, and the mapping of the server's 522
// violations onto the fields) and of the nest compatibility of an added vehicle (box: multirotor; launcher: fixed wing;
// vtol_pad: VTOL and multirotor; AWR-14 §20.8).
import type { CloneViolation, VehicleCatalogModelSummary } from '@awr/contracts/types'
import type { Range } from '@/ui/panels/targets/targetModel'
import type { ModelDetail, ModelSummary } from '@/stores/catalog'

export type Category = VehicleCatalogModelSummary['category']
export const CATEGORIES: readonly Category[] = ['multirotor', 'fixed_wing', 'vtol']

export function filterModels(models: readonly VehicleCatalogModelSummary[], cat: Category | 'all', q: string): VehicleCatalogModelSummary[] {
  const s = q.trim().toLowerCase()
  return models.filter((m) => (cat === 'all' || m.category === cat) && (!s || m.model_id.toLowerCase().includes(s) || (m.name_zh ?? '').toLowerCase().includes(s)))
}

/** one clone form field: dotted profile path, unit, range, which categories it applies to */
export interface CloneField { key: string; unit: string; range: Range; cats: readonly Category[]; digits: number }
const ALL: readonly Category[] = CATEGORIES
const FW: readonly Category[] = ['fixed_wing', 'vtol']
export const CLONE_FIELDS: readonly CloneField[] = [
  { key: 'mass_kg', unit: 'kg', range: { min: 0.2, max: 200 }, cats: ALL, digits: 3 },
  { key: 'mtow_kg', unit: 'kg', range: { min: 0.2, max: 200 }, cats: ALL, digits: 3 },
  { key: 'battery.cells', unit: '', range: { min: 3, max: 14 }, cats: ALL, digits: 0 },
  { key: 'battery.capacity_ah', unit: 'Ah', range: { min: 0.5, max: 60 }, cats: ALL, digits: 1 },
  { key: 'battery.hover_endurance_s', unit: 's', range: { min: 300, max: 7200 }, cats: ['multirotor'], digits: 0 },
  { key: 'airframe.c_d0', unit: '', range: { min: 0.02, max: 0.08 }, cats: FW, digits: 3 },
  { key: 'airframe.wing_area_m2', unit: 'm²', range: { min: 0.1, max: 3 }, cats: FW, digits: 2 },
  { key: 'airframe.bank_max_deg', unit: '°', range: { min: 15, max: 45 }, cats: FW, digits: 0 },
  { key: 'speed.cruise_mps', unit: 'm/s', range: { min: 1, max: 40 }, cats: ALL, digits: 1 },
  { key: 'speed.max_mps', unit: 'm/s', range: { min: 1, max: 40 }, cats: ALL, digits: 1 },
  { key: 'speed.min_mps', unit: 'm/s', range: { min: 1, max: 40 }, cats: FW, digits: 1 },
  { key: 'speed.climb_mps', unit: 'm/s', range: { min: 0.5, max: 10 }, cats: ALL, digits: 1 },
  { key: 'speed.sink_mps', unit: 'm/s', range: { min: 0.5, max: 10 }, cats: ALL, digits: 1 },
  { key: 'acoustic.lwa_hover_dba', unit: 'dB(A)', range: { min: 60, max: 110 }, cats: ALL, digits: 0 },
  { key: 'acoustic.bpf_hz', unit: 'Hz', range: { min: 20, max: 1000 }, cats: ALL, digits: 0 },
  { key: 'link.range_m', unit: 'm', range: { min: 500, max: 50000 }, cats: ALL, digits: 0 },
]

export type CloneValues = Record<string, number>

/** cross-field checks of the form (field -> rule key); the server's VH checks decide */
export function cloneErrors(v: CloneValues, cat: Category): Record<string, string> {
  const e: Record<string, string> = {}
  for (const f of CLONE_FIELDS) {
    if (!f.cats.includes(cat) || v[f.key] === undefined) continue
    const x = v[f.key]
    if (!Number.isFinite(x) || x < f.range.min || x > f.range.max) e[f.key] = 'range'
  }
  if (v.mass_kg !== undefined && v.mtow_kg !== undefined && v.mass_kg > v.mtow_kg) e.mass_kg = 'mass_le_mtow'
  if (v['speed.cruise_mps'] !== undefined && v['speed.max_mps'] !== undefined && v['speed.cruise_mps'] > v['speed.max_mps']) e['speed.cruise_mps'] = 'cruise_le_max'
  if (cat !== 'multirotor' && v['speed.min_mps'] !== undefined && v['speed.cruise_mps'] !== undefined && v['speed.min_mps'] >= v['speed.cruise_mps']) e['speed.min_mps'] = 'min_lt_cruise'
  return e
}

/** derived read-only values: nominal and usable energy (E_use = 0.85 E_nom; 3.7 V per cell nominal) */
export function derived(v: CloneValues): { wh: number; useWh: number } {
  const cells = v['battery.cells'] ?? Number.NaN
  const ah = v['battery.capacity_ah'] ?? Number.NaN
  const wh = cells * 3.7 * ah
  return { wh, useWh: 0.85 * wh }
}

/** 522 violations onto the form fields (unknown fields go to the form's top alert) */
export function mapViolations(vs: readonly CloneViolation[]): { fields: Record<string, string>; other: string[] } {
  const keys = new Set(CLONE_FIELDS.map((f) => f.key))
  const fields: Record<string, string> = {}
  const other: string[] = []
  for (const v of vs) {
    const k = v.field.replace(/^set\./, '')
    const lim = v.limit === undefined ? '' : typeof v.limit === 'number' || typeof v.limit === 'string' ? ` ${v.limit}` : ` ${JSON.stringify(v.limit)}`
    const msg = v.message_zh ?? `${v.rule}${lim}`
    if (keys.has(k)) fields[k] = msg
    else other.push(`${v.field}: ${msg}`)
  }
  return { fields, other }
}

/** nest kinds a model category may use (AWR-14 §20.8 "兼容") */
export function nestAccepts(kind: 'box' | 'launcher' | 'vtol_pad', cat: Category): boolean {
  if (kind === 'box') return cat === 'multirotor'
  if (kind === 'launcher') return cat === 'fixed_wing'
  return cat === 'vtol' || cat === 'multirotor'
}

/** fields the server never changes on a clone (CL-4: frame, cell count, chemistry): shown read-only */
export const LOCKED: ReadonlySet<string> = new Set(['battery.cells'])

/** the initial form values of a model: the summary's numbers and the detail's parameter rows of the same dotted paths
 *  (unknown fields stay empty and are not sent; pure, tests) */
export function baseValues(b: ModelSummary | ModelDetail, detail: Pick<ModelDetail, 'params_table'> | null = null): CloneValues {
  const v: CloneValues = {}
  const put = (k: string, x: unknown) => {
    if (typeof x === 'number' && Number.isFinite(x)) v[k] = x
  }
  const keys = new Set(CLONE_FIELDS.map((f) => f.key))
  for (const r of detail?.params_table ?? []) if (keys.has(r.key)) put(r.key, r.value)
  put('mass_kg', b.mass_kg)
  put('mtow_kg', b.mtow_kg)
  put('speed.cruise_mps', b.speed?.cruise_mps)
  put('speed.max_mps', b.speed?.max_mps)
  if (b.category !== 'multirotor') put('speed.min_mps', b.speed?.min_mps)
  put('link.range_m', b.link_range_m)
  return v
}

/** the fields that differ from the base values (the clone request only carries changes) */
export function changedFields(base: CloneValues, values: CloneValues): Record<string, number> {
  const out: Record<string, number> = {}
  for (const [k, v] of Object.entries(values)) if (Number.isFinite(v) && v !== base[k] && !LOCKED.has(k)) out[k] = v
  return out
}
