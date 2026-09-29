// Action registry (M15-FR-098, §7.1.1; AWR-14 §2.3, §6.10): menus, the command palette, hotkeys and context menus share
// one action per id with the same `when` guard; disabled actions explain why (disabledReasonKey, shown in the Tooltip,
// the palette row and the tool hint bar when a hotkey is refused).
import type { IconKey } from '@/ui/icons/registry'

export type ActionGroup = 'jump' | 'drone' | 'camera' | 'sim' | 'layer' | 'env' | 'panel' | 'settings'
export interface ActionDescriptor {
  id: string
  labelKey: string
  icon?: IconKey
  group: ActionGroup
  keywords?: string[]
  hotkey?: string
  /** hotkeys that also work while focus is in an editable element (only Mod+K) */
  allowInEditable?: boolean
  /** Esc must still work over modal dialogs */
  blockedByModal?: boolean
  when?: () => boolean
  /** i18n key of the reason when `when` fails; null keeps the hotkey silent */
  disabledReasonKey?: () => string | null
  run: () => void | Promise<void>
}

const actions = new Map<string, ActionDescriptor>()
export function registerAction(a: ActionDescriptor): () => void {
  actions.set(a.id, a)
  return () => {
    if (actions.get(a.id) === a) actions.delete(a.id)
  }
}
export const listActions = (group?: ActionGroup): readonly ActionDescriptor[] =>
  [...actions.values()].filter((a) => !group || a.group === group)
export const getAction = (id: string): ActionDescriptor | undefined => actions.get(id)

/** run an action when its guard allows; false when refused (the caller may show disabledReasonKey) */
export function runAction(id: string): boolean {
  const a = actions.get(id)
  if (!a || (a.when && !a.when())) return false
  void a.run()
  return true
}
