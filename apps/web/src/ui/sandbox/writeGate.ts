// Write gate of the D2 management pages (ADR-111; AWR-04 §10.2; M15-FR-202; D2.1 ADR-123): writes are offered on the sandbox
// surface (this browser holds the sid's token: operator) and wherever the connection's role is operator or admin (the
// D2.1 full-function identity on the shared show, local runs), never while the domain data is stale (the D2.1
// energy-saving mode reclaimed the session, or the link is down: M20 `stale`). Hiding a control is not the security
// boundary: the server applies the same policy (ADR-093).
import { useStore } from 'zustand'
import { surfaceStore } from '@/app/shell/surface'
import { taskingStore, useTasking } from '@/stores/tasking'

/** pure rule (tests) */
export function d2WriteAllowed(surface: string, canEdit: boolean, stale: boolean): boolean {
  return !stale && (surface === 'sandbox' || canEdit)
}

/** React hook of the gate (the tasking feed attaches with it) */
export function useD2Write(): boolean {
  const surface = useStore(surfaceStore, (s) => s.surface)
  const canEdit = useTasking((s) => s.canEdit)
  const stale = useTasking((s) => s.stale)
  return d2WriteAllowed(surface, canEdit, stale)
}

/** the gate outside React (action guards) */
export function d2WriteNow(): boolean {
  const t = taskingStore.getState()
  return d2WriteAllowed(surfaceStore.getState().surface, t.canEdit, t.stale)
}
