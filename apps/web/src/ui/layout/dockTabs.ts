// Dock tabs of the D2 surfaces (AWR-14 §20.5.2; M15-FR-214): pure helper of the bottom Dock.
import type { PanelDescriptor } from '@/ui/panels/registry'
import type { Surface } from '@/app/shell/surface'

const TASKING_TABS = new Set(['tasks', 'relay'])
/**
 * D2 (AWR-14 §20.5.2): the task and relay tabs replace the D1 mission tab on the D2 surfaces (shared show, static browsing,
 * sandbox) and on local runs with the `tasking` capability; elsewhere the D1 mission tab stays (pure; tests)
 */
export function dockPanelsFor(panels: readonly PanelDescriptor[], surface: Surface, caps: readonly string[]): PanelDescriptor[] {
  const d2 = surface !== 'local' || caps.includes('tasking')
  return panels.filter((p) => (d2 ? p.id !== 'mission' : !TASKING_TABS.has(p.id)))
}
