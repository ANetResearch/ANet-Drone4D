// World context of the viewport (M06 §7.3; AWR-16 §3, §7): coordinate.json (ground.zM, relief, raw bytes for the
// flight60 checksum), world.json bounds, semantic/zones.geojson (awr.zones.v1). Owner: M06.
// Loaded after the point cloud opened the world (never on the TTFP path); files are static (?v=contentVersion is
// immutable, AWR-17 §5.3). A world switch clears world-bound state (zones, GoTo marker, trails; M06 §6.14).
import { parseZones, type ZoneFeature } from '@/engine'
import type { WorldContext } from './session'

export interface WorldFiles { ctx: WorldContext; zones: ZoneFeature[] }

export async function loadWorldFiles(worldId: string, contentVersion: string, signal?: AbortSignal, origin = typeof location !== 'undefined' ? location.origin : ''): Promise<WorldFiles> {
  const base = `${origin}/worlds/${encodeURIComponent(worldId)}/`
  const v = `?v=${encodeURIComponent(contentVersion)}`
  const [wr, cr] = await Promise.all([fetch(`${base}world.json`, { signal, cache: 'no-cache' }), fetch(`${base}coordinate.json${v}`, { signal })])
  const world = wr.ok ? ((await wr.json()) as { bounds?: { min: [number, number, number]; max: [number, number, number] }; semantic?: unknown }) : {}
  const coordinateBytes = cr.ok ? await cr.arrayBuffer() : null
  let groundZ = 0
  let reliefLow = 0
  if (coordinateBytes) {
    try {
      const c = JSON.parse(new TextDecoder().decode(coordinateBytes)) as { ground?: { zM?: number; reliefP1P99M?: [number, number] } }
      groundZ = c.ground?.zM ?? 0
      reliefLow = c.ground?.reliefP1P99M?.[0] ?? 0
    } catch {
      /* keep defaults */
    }
  }
  let zones: ZoneFeature[] = []
  try {
    const zr = await fetch(`${base}semantic/zones.geojson${v}`, { signal })
    if (zr.ok) zones = parseZones(await zr.json())
  } catch {
    zones = []
  }
  return {
    ctx: {
      worldId, contentVersion, groundZ, reliefLow, coordinateBytes,
      boundsMin: world.bounds?.min ?? null, boundsMax: world.bounds?.max ?? null,
    },
    zones,
  }
}

/** M06-FR-028: GPU positions are float32 ENU relative to WorldRoot; beyond 10 km horizontal radius M06-E016 warns */
export const WORLD_RADIUS_WARN_M = 10_000

/** largest horizontal distance of the world bounds' corners from the ENU origin (m); 0 without bounds */
export function worldRadiusM(ctx: { boundsMin: readonly number[] | null; boundsMax: readonly number[] | null }): number {
  const a = ctx.boundsMin
  const b = ctx.boundsMax
  if (!a || !b) return 0
  let r = 0
  for (const x of [a[0], b[0]]) for (const y of [a[1], b[1]]) r = Math.max(r, Math.hypot(x, y))
  return r
}
