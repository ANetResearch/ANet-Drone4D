// UI-internal overlay state (M15-FR-033): command palette, shortcut help and about dialog; the settings dialog is driven by
// the ?settings=<tab> query (deep link, M15-FR-026). Written on user actions only.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'

export interface OverlaysState { palette: boolean; help: boolean; about: boolean; alarms: boolean }
export const overlaysStore = createAwrStore<OverlaysState>('ui.overlays', () => ({ palette: false, help: false, about: false, alarms: false }))
export const overlays = {
  set(k: keyof OverlaysState, v: boolean): void {
    if (overlaysStore.getState()[k] !== v) overlaysStore.setState({ [k]: v } as Partial<OverlaysState>)
  },
  toggle(k: keyof OverlaysState): void {
    overlaysStore.setState({ [k]: !overlaysStore.getState()[k] } as Partial<OverlaysState>)
  },
}
export const useOverlays = <T,>(sel: (s: OverlaysState) => T): T => useStore(overlaysStore, sel)
