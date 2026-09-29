// Event marker classes (M12 §7.5.2, §8.3; FR-017, FR-019). Owner: M12. The rule table is the draft of the contract
// packages/contracts/rec/markers.json (awr.rec.markers.v1, M12 drafts, M00 owns; not yet generated): rules match in
// order, first hit wins, so level >= 2 events fall into warning or critical before any type rule. The backend twin is
// python/awr/recorder/sidecar.py (same table; parity test tests/recorder/test_sidecar.py and apps/web/tests/time).
// Classes are the .evx `marker` low nibble; bit 4 marks a superseded (rolled-back) record.
export const MarkerClass = { NONE: 0, LIFECYCLE: 1, ROUTE: 2, WARNING: 3, CRITICAL: 4, SYSTEM: 5 } as const
export const MARKER_SUPERSEDED = 0x10
export const MARKER_CLASS_MASK = 0x0f

/** draw priority per class: one marker per pixel column, the highest priority wins (critical > warning > route >
 * lifecycle > system > none) */
export const MARKER_PRIORITY: readonly number[] = [0, 2, 3, 4, 5, 1]

export interface MarkerRule {
  level_min?: number
  type?: string
  type_prefix?: readonly string[]
  'data.to'?: readonly string[]
  'data.op'?: readonly string[]
  class: 'none' | 'lifecycle' | 'route' | 'warning' | 'critical' | 'system'
}

/** awr.rec.markers.v1 draft (M12 §7.5.2) */
export const MARKER_RULES = {
  schema: 'awr.rec.markers.v1',
  version: 1,
  rules: [
    { level_min: 3, class: 'critical' },
    { level_min: 2, class: 'warning' },
    { type: 'uav.state', 'data.to': ['TAKING_OFF', 'LANDED'], class: 'lifecycle' },
    { type: 'sim.vehicle.state', class: 'lifecycle' },
    { type: 'cmd.accepted', 'data.op': ['goto', 'follow_path', 'orbit', 'rtl', 'land'], class: 'route' },
    { type: 'mission.state', class: 'route' },
    { type_prefix: ['sim.reset', 'sim.started', 'rec.', 'session.', 'scenario.'], class: 'system' },
  ] as readonly MarkerRule[],
  default: 'none',
} as const

const CODE: Readonly<Record<MarkerRule['class'], number>> = {
  none: MarkerClass.NONE, lifecycle: MarkerClass.LIFECYCLE, route: MarkerClass.ROUTE, warning: MarkerClass.WARNING,
  critical: MarkerClass.CRITICAL, system: MarkerClass.SYSTEM,
}

const strIn = (v: unknown, set: readonly string[]): boolean => typeof v === 'string' && set.includes(v)

/** marker class of an event (WS names `type`, `level`; the bus names `kind`, `severity` map to the same) */
export function markerClassOf(type: string, level: number, data?: Record<string, unknown> | null): number {
  for (const r of MARKER_RULES.rules) {
    if (r.level_min !== undefined && level < r.level_min) continue
    if (r.type !== undefined && r.type !== type) continue
    if (r.type_prefix !== undefined && !r.type_prefix.some((p) => type.startsWith(p))) continue
    if (r['data.to'] !== undefined && !strIn(data?.to, r['data.to'])) continue
    if (r['data.op'] !== undefined && !strIn(data?.op, r['data.op'])) continue
    return CODE[r.class]
  }
  return MarkerClass.NONE
}
