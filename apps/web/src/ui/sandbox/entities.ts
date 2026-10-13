// Entity selection of the D2 management panels (AWR-14 §20.2.2 `tgt`, §20.5.2, §20.7.1): the selected target and the
// selected nest are kept apart from the vehicle selection set (selecting a target never changes the focused vehicle), and
// the right rail's entity switch (drones, targets, nests) with its page stack (list, detail). `?ent=` and `?tgt=` deep
// links are applied once at start (ui/shell/urlSync.ts keeps the D1 parameters).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { prefs, prefsStore } from '@/stores/prefs'

export type Entity = 'drones' | 'targets' | 'nests'
export interface EntitySelection { target: string | null; nest: string | null; version: number }
export const entityStore = createAwrStore<EntitySelection>('ui.entities', () => ({ target: null, nest: null, version: 0 }))
export const useEntities = <T,>(sel: (s: EntitySelection) => T): T => useStore(entityStore, sel)

const bump = (p: Partial<EntitySelection>): void => entityStore.setState({ ...p, version: entityStore.getState().version + 1 })

export const entities = {
  /** switch the rail's entity list (page 1) */
  show(ent: Entity): void {
    prefs.setLayout({ right: { ent, page: 'list', open: true } })
  },
  /** select a target and open its detail page */
  openTarget(id: string | null): void {
    bump({ target: id })
    prefs.setLayout({ right: { ent: 'targets', page: id ? 'detail' : 'list', open: true } })
  },
  openNest(id: string | null): void {
    bump({ nest: id })
    prefs.setLayout({ right: { ent: 'nests', page: id ? 'detail' : 'list', open: true } })
  },
  /** Esc chain: the target selection is cleared before the vehicle selection (AWR-14 §20.7.1) */
  clearTarget(): boolean {
    if (!entityStore.getState().target) return false
    bump({ target: null })
    return true
  },
  back(): void {
    prefs.setLayout({ right: { page: 'list' } })
  },
  current(): Entity {
    return prefsStore.getState().layout.right.ent ?? 'drones'
  },
}

/** `?ent=targets&tgt=person-01` and `?panel=targets|nests|tasks|task-new|relay` deep links (read once) */
export function applyEntityDeepLink(q: URLSearchParams): void {
  const ent = q.get('ent')
  const tgt = q.get('tgt')
  const panel = q.get('panel')
  if (tgt && /^[a-z0-9][a-z0-9_-]{0,63}$/i.test(tgt)) entities.openTarget(tgt)
  else if (ent === 'targets' || ent === 'nests' || ent === 'drones') entities.show(ent)
  else if (panel === 'targets' || panel === 'nests') entities.show(panel)
  // AWR-14 §20.2.2: `tasks` and `relay` open the Dock tabs, `task-new` the right rail's new task page (read-only for viewers)
  else if (panel === 'tasks' || panel === 'relay') prefs.setLayout({ dock: { open: true, tab: panel } })
  else if (panel === 'task-new') prefs.setLayout({ right: { page: 'task', open: true } })
}
