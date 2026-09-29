// queryOptions factories (M15-FR-038; AWR-17 §4.2): fetchers come from net/api.ts (M11).
// R04 GET /api/worlds answers {items, next_cursor} with snake_case fields (AWR-17 §4.3.2; SK-B-to-M15 item 1); the
// select maps them to the camelCase WorldListItem the World Hub and the WORLD panel read. TTFP is not part of the REST
// contract: the last TTFP per world is a local record (awr.ui.ttfp.v1, written when a world reveals).
import { queryOptions } from '@tanstack/react-query'
import { apiGet } from '@/net/api'
import { qk } from './keys'

export type WorldStatus = 'ready' | 'missing' | 'building' | 'stale' | 'failed' | 'invalid'
export interface WorldListItem {
  id: string
  name?: string
  nameZh?: string
  status?: WorldStatus
  stats?: { points?: number; nodes?: number; bytes?: number; roots?: number; maxHeightM?: number; ttfpMs?: number }
  levelsPoints?: number[]
  anchorKind?: 'geodetic' | 'synthetic'
  georeferenced?: boolean
  contentVersion?: string
  scaleStatus?: string
  firstScreen?: { points?: number; bytes?: number }
  defaultScenarioId?: string | null
  inUse?: boolean
}

/** wire item of R04 (snake_case) */
export interface WorldWire {
  id: string
  name?: string
  name_zh?: string
  status?: string
  content_version?: string
  scale_status?: string
  anchor_kind?: string
  georeferenced?: boolean
  points?: number
  bytes?: number
  roots?: number
  octree_bytes?: number
  node_count?: number
  levels_points?: number[]
  max_height_m?: number
  first_screen?: { points?: number; bytes?: number }
  default_scenario_id?: string | null
  in_use?: boolean
}

const STATUSES: readonly WorldStatus[] = ['ready', 'missing', 'building', 'stale', 'failed', 'invalid']
const TTFP_KEY = 'awr.ui.ttfp.v1'
function localTtfp(): Record<string, number> {
  try {
    const raw = globalThis.localStorage?.getItem(TTFP_KEY)
    const v = raw ? (JSON.parse(raw) as unknown) : null
    return v && typeof v === 'object' ? (v as Record<string, number>) : {}
  } catch {
    return {}
  }
}
/** remember the TTFP of a world on this browser (World Hub "last TTFP") */
export function recordTtfp(worldId: string, ms: number): void {
  if (!Number.isFinite(ms) || ms <= 0) return
  try {
    const all = localTtfp()
    all[worldId] = Math.round(ms)
    globalThis.localStorage?.setItem(TTFP_KEY, JSON.stringify(all))
  } catch {
    // storage unavailable: nothing to remember
  }
}

/** snake_case wire item -> view item (pure; exported for tests) */
export function mapWorld(w: WorldWire, ttfp: Record<string, number> = {}): WorldListItem {
  const status = STATUSES.includes(w.status as WorldStatus) ? (w.status as WorldStatus) : undefined
  const anchor = w.anchor_kind === 'geodetic' || w.anchor_kind === 'synthetic' ? w.anchor_kind : undefined
  return {
    id: w.id, name: w.name, nameZh: w.name_zh, status, contentVersion: w.content_version, scaleStatus: w.scale_status,
    anchorKind: anchor, georeferenced: w.georeferenced, levelsPoints: Array.isArray(w.levels_points) ? w.levels_points : undefined,
    stats: { points: w.points, nodes: w.node_count, bytes: w.bytes, roots: w.roots, maxHeightM: w.max_height_m, ttfpMs: ttfp[w.id] },
    firstScreen: w.first_screen, defaultScenarioId: w.default_scenario_id ?? null, inUse: w.in_use,
  }
}

export function mapWorldList(d: { items?: WorldWire[] } | WorldWire[] | { worlds?: WorldWire[] }): WorldListItem[] {
  const list = Array.isArray(d) ? d : ('items' in d && Array.isArray(d.items) ? d.items : 'worlds' in d && Array.isArray(d.worlds) ? d.worlds : [])
  const ttfp = localTtfp()
  return list.filter((w) => w && typeof w.id === 'string').map((w) => mapWorld(w, ttfp))
}

export const worldsQuery = () =>
  queryOptions({
    queryKey: qk.worlds(),
    queryFn: ({ signal }) => apiGet<{ items?: WorldWire[]; next_cursor?: string | null }>('/api/worlds?limit=100', { signal }),
    select: mapWorldList,
  })
