// /settings opens the settings dialog over the current world (the dialog itself is driven by ?settings=<tab>,
// M15-FR-026 deep link).
import type { RouteDef } from '@/app/router/router'
import { prefsStore } from '@/stores/prefs'
import { parseSettings } from '@/app/router/search'
import { DEMO_PUBLIC, DEMO_WORLD } from '@/lib/demo'
import { DEFAULT_WORLD } from './index'

export const route: RouteDef = {
  id: 'settings', pattern: '/settings', layer: 'sandbox', d1: 'core',
  redirect: (_p, search) => `/world/${DEMO_PUBLIC ? DEMO_WORLD : prefsStore.getState().ui.lastWorld ?? DEFAULT_WORLD}?settings=${parseSettings(search) ?? 'general'}`,
}
