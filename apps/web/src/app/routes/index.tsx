// "/" redirects to the last world or the default world (M15-FR-003; AWR-03 §8.5: /world/shenzhen).
import type { RouteDef } from '@/app/router/router'
import { prefsStore } from '@/stores/prefs'

export const DEFAULT_WORLD = 'shenzhen'
export const route: RouteDef = {
  id: 'root', pattern: '/', layer: 'sandbox', d1: 'core',
  redirect: (_p, search) => `/world/${prefsStore.getState().ui.lastWorld ?? DEFAULT_WORLD}${search.toString() ? `?${search.toString()}` : ''}`,
}
