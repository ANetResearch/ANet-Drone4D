// Panel registry (M15-FR-020, §7.1.1; AWR-14 §3.5; AWR-03 §4.3 extension point): modules add panels with registerPanel()
// without editing M15 files; `ext` panels are only registered when their D1-ext feature ships; a panel lives in one slot;
// an empty slot folds its rail. Panel bodies read domain stores through selectors (<= 10 Hz).
import type * as React from 'react'
import type { IconKey } from '@/ui/icons/registry'

export type DockSlot = 'left' | 'right' | 'bottom'
export interface PanelCtx {
  role: 'viewer' | 'operator' | 'admin'
  seat: 'held' | 'none' | 'other'
  mode: 'live' | 'replay'
  tier: 'A' | 'B' | 'S'
  caps: { clock: { pausable: boolean; maxSpeed: number; steppable: boolean } }
}
export interface PanelDescriptor {
  id: string
  titleKey: string
  icon: IconKey
  home: DockSlot
  allowed: readonly DockSlot[]
  minSize: { w: number; h: number }
  layer: 'core' | 'ext'
  /** order inside the slot (left groups top to bottom, Dock tabs left to right) */
  order?: number
  /** streaming chart slots this panel may use while visible (Tier S scheduling hint) */
  streaming?: number
  when?: (ctx: PanelCtx) => boolean
  render: () => React.ReactNode
}

const panels = new Map<string, PanelDescriptor>()
const slotOverride = new Map<string, DockSlot>()
const listeners = new Set<() => void>()
let snapshot: readonly PanelDescriptor[] = []
const notify = () => {
  snapshot = [...panels.values()]
  for (const l of listeners) l()
}

export function registerPanel(d: PanelDescriptor): () => void {
  if (!d.allowed.includes(d.home)) throw new Error(`panel ${d.id}: home slot ${d.home} is not in allowed`)
  panels.set(d.id, d)
  notify()
  return () => {
    if (panels.get(d.id) === d) {
      panels.delete(d.id)
      notify()
    }
  }
}

export const slotOf = (d: PanelDescriptor): DockSlot => slotOverride.get(d.id) ?? d.home

/** docking (P1): move a panel to another allowed slot */
export function dockPanel(id: string, slot: DockSlot): boolean {
  const d = panels.get(id)
  if (!d || !d.allowed.includes(slot)) return false
  slotOverride.set(id, slot)
  notify()
  return true
}

export function listPanels(slot?: DockSlot): readonly PanelDescriptor[] {
  const all = snapshot
  const list = slot ? all.filter((d) => slotOf(d) === slot) : all
  return [...list].sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.id.localeCompare(b.id))
}

export function getPanel(id: string): PanelDescriptor | undefined {
  return panels.get(id)
}

export function subscribePanels(cb: () => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}
export const panelsSnapshot = (): readonly PanelDescriptor[] => snapshot
