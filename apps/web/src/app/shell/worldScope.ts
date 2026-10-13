// World scope binding (D2.2-W1; ADR-111; AWR-14 §20.2.3): feeds stores/worldScope with the world the view shows and the
// realtime source of the run-time surface. The view's world is the world the viewport opened (stores/world, written when the
// point cloud layer opens it), before that, and without a viewport (UI-only test builds), the surface's world (the route's
// world, or the sandbox session's world). The source is the realtime source the surface belongs to, independent of the link
// state: the shared show and the local build keep their source while the power-save controller closes the link, a parked
// sandbox keeps its sid; static browsing and an unavailable sandbox have none. Called once from app/shell/install.ts.
import { worldScope } from '@/stores/worldScope'
import { worldStore } from '@/stores/world'
import { surfaceStore, type UiSurface } from './surface'

/**
 * realtime source key of a surface: 'shared:<world>' | 'local' | 'sandbox:<sid>' | null (no realtime link belongs to
 * the view). D2.2-W2: the shared show now has a different realtime source per world (synthcity live, each
 * UrbanScene3D city its own recorded show, app/shell/surface.ts resolveSurface), so the world is part of the key —
 * switching between two shown worlds must reconnect and reset the same as switching data source.
 */
export function sourceKeyOf(s: Pick<UiSurface, 'surface' | 'sid' | 'worldId'>): string | null {
  if (s.surface === 'shared') return `shared:${s.worldId ?? ''}`
  if (s.surface === 'local') return 'local'
  if (s.surface === 'sandbox' && s.sid) return `sandbox:${s.sid}`
  return null
}

/** the view's world: the opened world of the viewport, else the surface's world */
export function viewWorldOf(opened: string | null, surfaceWorld: string | null): string | null {
  return opened ?? surfaceWorld
}

let installed = false
/** follow the surface and the opened world (start-up); returns the detach function */
export function installWorldScope(): () => void {
  if (installed) return () => {}
  installed = true
  const sync = (): void => {
    const s = surfaceStore.getState()
    worldScope.setView(viewWorldOf(worldStore.getState().worldId, s.worldId), sourceKeyOf(s))
  }
  const offSurface = surfaceStore.subscribe(sync)
  const offWorld = worldStore.subscribe((s, p) => {
    if (s.worldId !== p.worldId) sync()
  })
  sync()
  return () => {
    installed = false
    offSurface()
    offWorld()
  }
}
