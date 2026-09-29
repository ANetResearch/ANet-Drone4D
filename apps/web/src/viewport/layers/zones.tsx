// Restricted-zone adapter, ZonesLayer (M06 §6.10, FR-047, AC-036; AWR-16 §7). Owner: M06.
// When the point cloud has opened a world (vp.world), loads coordinate.json and semantic/zones.geojson (worldData.ts),
// stores the world context (ground height for clamps and patches, bounds, coordinate bytes for flight60) and builds
// the zones once (walls, solid and dashed top outlines, vertical edges: <= 4 draws). A world switch rebuilds them.
import { useEffect } from 'react'
import { ZonesLayer as Zones } from '@/engine'
import type { RenderBackend } from '../renderer'
import { registerLayer } from './registry'
import { useVp, vp } from '../session'
import { loadWorldFiles, worldRadiusM, WORLD_RADIUS_WARN_M } from '../worldData'
import { applyLayerVisibility } from '../bindings'

export function ZonesLayer({ be }: { be: RenderBackend }) {
  const worldId = useVp((s) => s.world?.worldId ?? null)
  const contentVersion = useVp((s) => s.world?.contentVersion ?? '')
  useEffect(() => {
    const z = new Zones(be.tier)
    vp.zones = z
    const off = registerLayer({
      id: 'zones', owner: 'M06', perfKey: 'trails', root: z.root, channel: 0,
      drawCount: () => (z.root.visible ? z.drawCount() : 0),
      warmupVariants: () => {
        // a one-quad stand-in zone so every zone program compiles under the mask
        const empty = z.zones.length === 0
        return [{
          object: z.root,
          before: () => {
            if (empty) z.build([{ id: '__warm', kind: 'nofly', ring: [[0, 0], [1, 0], [1, 1]], minZ: 0, maxZ: 1, label: '' },
              { id: '__warm2', kind: 'restricted', ring: [[0, 0], [1, 0], [1, 1]], minZ: 0, maxZ: 1, label: '' }], 0, 1)
          },
          after: () => {
            if (empty) z.build([], 0, 1)
          },
        }]
      },
      setVisible: (v) => {
        z.root.visible = v
      },
      dispose: () => {},
    })
    applyLayerVisibility()
    return () => {
      off()
      z.dispose()
      if (vp.zones === z) vp.zones = null
    }
  }, [be])
  useEffect(() => {
    if (!worldId) return
    const ac = new AbortController()
    loadWorldFiles(worldId, contentVersion, ac.signal).then((f) => {
      if (ac.signal.aborted) return
      vp.worldCtx = f.ctx
      const radius = worldRadiusM(f.ctx)
      if (radius > WORLD_RADIUS_WARN_M) console.warn(`M06-E016 world ${worldId} horizontal radius ${Math.round(radius)} m > ${WORLD_RADIUS_WARN_M} m: float32 ENU precision degrades`)
      const top = f.ctx.boundsMax?.[2] ?? f.ctx.groundZ + 300
      const low = f.ctx.groundZ + f.ctx.reliefLow
      vp.zones?.build(f.zones, low, top)
      vp.changed()
    }).catch(() => {
      /* the overlay is optional: missing files leave the zones empty */
    })
    return () => ac.abort()
  }, [worldId, contentVersion])
  return null
}
