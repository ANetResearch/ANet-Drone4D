// Panel host (M15 §9.2): renders the registered panels of a slot with one error boundary per panel (M15-FR-008: a failing
// panel only replaces its own body), and tells the LfScheduler when a panel is hidden (inactive Dock tab, folded rail),
// because hidden tabs can stay in the layout tree (M15 §6.8.2).
import * as React from 'react'
import type { StoreApi } from 'zustand/vanilla'
import { useT } from '@/app/i18n'
import { PanelErrorBoundary } from '@/app/providers/ErrorBoundaries'
import { lfScheduler } from '@/ui/lf/scheduler'
import { listPanels, panelsSnapshot, subscribePanels, type DockSlot, type PanelDescriptor } from './registry'

/**
 * whether the panel body around this component is on screen (open rail and active tab). Panels whose store updates run
 * at the bridge or UI tick rate read it to stop re-rendering while folded away (ADR-069; D1-AC-27): a folded Dock kept the
 * event table re-rendering on every 4 Hz bridge flush, inside the flush task.
 */
export const PanelVisibleContext = React.createContext(true)
export const usePanelVisible = (): boolean => React.useContext(PanelVisibleContext)

/**
 * the state of a store while the panel is visible, held at the last visible state while it is folded away: a store
 * write then does not re-render the hidden panel; becoming visible reads the store again (one render).
 */
export function useVisibleState<S>(store: StoreApi<S>): S {
  const visible = usePanelVisible()
  const held = React.useRef<S | null>(null)
  const get = React.useCallback((): S => {
    if (visible || held.current === null) held.current = store.getState()
    return held.current
  }, [store, visible])
  return React.useSyncExternalStore(store.subscribe, get, get)
}

export function usePanels(slot: DockSlot): readonly PanelDescriptor[] {
  const snap = React.useSyncExternalStore(subscribePanels, panelsSnapshot, panelsSnapshot)
  return React.useMemo(() => (snap ? listPanels(slot) : []), [snap, slot])
}

/**
 * a panel body with its error boundary and visibility wiring; `fill` gives it the height of its slot cell, so that a
 * panel with its own scroller (the virtualised fleet list) is height-constrained instead of growing with its rows
 */
export function PanelBody({ panel, visible = true, fill = false }: { panel: PanelDescriptor; visible?: boolean; fill?: boolean }) {
  const t = useT()
  const ref = React.useRef<HTMLDivElement>(null)
  React.useEffect(() => {
    if (ref.current) lfScheduler.setVisible(ref.current, visible)
  }, [visible])
  return (
    <div ref={ref} data-panel={panel.id} className={fill ? 'flex h-full min-h-0 flex-col gap-2' : 'flex min-h-0 flex-col gap-2'}>
      <PanelVisibleContext value={visible}>
        <PanelErrorBoundary title={t(panel.titleKey)}>{panel.render()}</PanelErrorBoundary>
      </PanelVisibleContext>
    </div>
  )
}
