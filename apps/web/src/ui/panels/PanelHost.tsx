// Panel host (M15 §9.2): renders the registered panels of a slot with one error boundary per panel (M15-FR-008: a failing
// panel only replaces its own body), and tells the LfScheduler when a panel is hidden (inactive Dock tab, folded rail),
// because hidden tabs can stay in the layout tree (M15 §6.8.2).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { PanelErrorBoundary } from '@/app/providers/ErrorBoundaries'
import { lfScheduler } from '@/ui/lf/scheduler'
import { listPanels, panelsSnapshot, subscribePanels, type DockSlot, type PanelDescriptor } from './registry'

export function usePanels(slot: DockSlot): readonly PanelDescriptor[] {
  const snap = React.useSyncExternalStore(subscribePanels, panelsSnapshot, panelsSnapshot)
  return React.useMemo(() => (snap ? listPanels(slot) : []), [snap, slot])
}

/** a panel body with its error boundary and visibility wiring */
export function PanelBody({ panel, visible = true }: { panel: PanelDescriptor; visible?: boolean }) {
  const t = useT()
  const ref = React.useRef<HTMLDivElement>(null)
  React.useEffect(() => {
    if (ref.current) lfScheduler.setVisible(ref.current, visible)
  }, [visible])
  return (
    <div ref={ref} data-panel={panel.id} className="flex min-h-0 flex-col gap-2">
      <PanelErrorBoundary title={t(panel.titleKey)}>{panel.render()}</PanelErrorBoundary>
    </div>
  )
}
