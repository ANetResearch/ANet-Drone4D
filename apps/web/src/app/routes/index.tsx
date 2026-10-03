// "/" redirects to the last world or the default world (M15-FR-003; AWR-03 §8.5: /world/shenzhen). Without a last world
// the redirect is provisional: the backend may run the generated demo city instead (no UrbanScene3D data, ADR-077), so
// the first serverInfo replaces /world/shenzhen with the session world (ui/shell/defaultWorld.ts).
import type { RouteDef } from '@/app/router/router'
import { markDefaultRedirect } from '@/ui/shell/defaultWorld'
import { prefsStore } from '@/stores/prefs'

export const DEFAULT_WORLD = 'shenzhen'
export const route: RouteDef = {
  id: 'root', pattern: '/', layer: 'sandbox', d1: 'core',
  redirect: (_p, search) => {
    const last = prefsStore.getState().ui.lastWorld
    if (!last) markDefaultRedirect(DEFAULT_WORLD)
    return `/world/${last ?? DEFAULT_WORLD}${search.toString() ? `?${search.toString()}` : ''}`
  },
}
