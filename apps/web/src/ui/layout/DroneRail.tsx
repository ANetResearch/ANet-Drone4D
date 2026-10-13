// Right rail DRONES (M15-FR-012, FR-021, FR-024; AWR-14 §4.4): floating offcanvas Sidebar in its RailHost with a page
// stack (Tabs without a visible list, TabsPanels single-cell grid, transitions.dev 08): the fleet list, the vehicle
// detail and the route and area editor (page 3, D1-AC-17). Right-slot panels are the pages. D2: the "new task" page
// (page 3, `task-new`, AWR-14 §20.9.1) draws in the viewport, so it is a rail page and not a Sheet.
// D2 (AWR-14 §20.5.2; M15-FR-211 to FR-213; UX-FR-112): the root page carries the entity switch (ToggleGroup with the
// sliding indicator: vehicles, targets, nests) on the D2 surfaces (shared show, static browsing, sandbox; the local build
// when its run has the `targets` capability); each entity has its list and detail page. Switching only changes the visible
// page: the other lists stay mounted (their subscriptions are kept, the hidden ones stop re-rendering).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { prefs, usePrefs } from '@/stores/prefs'
import { LAYOUT } from '@/lib/tokens/input.gen'
import { Sidebar, SidebarContent, SidebarHeader, SidebarProvider } from '@/ui/components/ui/sidebar'
import { Tabs, TabsContent, TabsPanels } from '@/ui/components/ui/tabs'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { PanelBody, usePanels } from '@/ui/panels/PanelHost'
import { useSurface } from '@/app/shell/surface'
import { useConnView } from '@/ui/shell/connView'
import { entities, type Entity } from '@/ui/sandbox/entities'
import { RailHost } from './RailHost'

/** right rail pages (AWR-14 §3.2 (d)): per entity a list and a detail page; the route and area editor */
const LIST: Readonly<Record<Entity, string>> = { drones: 'drones', targets: 'targets', nests: 'nests' }
const DETAIL: Readonly<Record<Entity, string>> = { drones: 'drone-detail', targets: 'target-detail', nests: 'nest-detail' }
const ENT_ICON: Readonly<Record<Entity, IconKey>> = { drones: 'drone.quad', targets: 'target.person', nests: 'nest.box' }
const ENTS: readonly Entity[] = ['drones', 'targets', 'nests']

/** the page panel id of a rail state (pure; tests) */
export function pagePanel(page: 'list' | 'detail' | 'edit' | 'task', ent: Entity): string {
  return page === 'edit' ? 'mission-edit' : page === 'task' ? 'task-new' : page === 'detail' ? DETAIL[ent] : LIST[ent]
}
/** the rail state of a page panel id */
function stateOf(id: string): { page: 'list' | 'detail' | 'edit' | 'task'; ent?: Entity } {
  if (id === 'mission-edit') return { page: 'edit' }
  if (id === 'task-new') return { page: 'task' }
  for (const e of ENTS) {
    if (DETAIL[e] === id) return { page: 'detail', ent: e }
    if (LIST[e] === id) return { page: 'list', ent: e }
  }
  return { page: 'list' }
}

export function DroneRail() {
  const t = useT()
  const right = usePrefs((s) => s.layout.right)
  const panels = usePanels('right')
  const surface = useSurface((s) => s.surface)
  const caps = useConnView((s) => s.caps)
  const d2 = surface !== 'local' || caps.includes('targets')
  const ent: Entity = d2 ? (right.ent ?? 'drones') : 'drones'
  const wanted = pagePanel(right.page, ent)
  const value = panels.some((p) => p.id === wanted) ? wanted : (panels[0]?.id ?? 'drones')
  const current = panels.find((p) => p.id === value)
  const root = right.page === 'list'
  return (
    <RailHost side="right" open={right.open && panels.length > 0} size={right.width} minSize={LAYOUT.rightMinPx} maxSize={LAYOUT.rightMaxPx}
      onResized={(w) => prefs.setLayout({ right: { width: w } })} label={t('rail.right')}>
      <SidebarProvider open={right.open} onOpenChange={(open) => prefs.setLayout({ right: { open } })} className="h-full min-h-0">
        <Sidebar side="right" variant="floating" collapsible="offcanvas">
          <SidebarHeader className="flex-row items-center gap-1.5 px-3 pt-3 text-hud-cap font-semibold uppercase text-muted-foreground">
            {d2 && root ? (
              <ToggleGroup spacing={0} size="sm" value={[ent]} aria-label={t('rail.entities')} data-entity-switch={ent} className="normal-case"
                onValueChange={(v: unknown[]) => ENTS.includes(v[0] as Entity) && entities.show(v[0] as Entity)}>
                {ENTS.map((e) => (
                  <ToggleGroupItem key={e} value={e} data-entity={e}>
                    <Icon icon={ENT_ICON[e]} data-icon="inline-start" />
                    {t(`rail.ent.${e}`)}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            ) : (
              <>
                {current ? <Icon icon={current.icon} /> : null}
                {current ? t(current.titleKey) : null}
              </>
            )}
          </SidebarHeader>
          <SidebarContent className="px-2 pb-2">
            <Tabs value={value} onValueChange={(v) => prefs.setLayout({ right: stateOf(String(v)) })} className="min-h-0 flex-1">
              {/* one row of the cell height: the fleet list scrolls inside its own virtualised viewport (rendered rows <=
                  visible + overscan, M15-FR-024, D1-AC-27); without it the cell grew with the rows and every row rendered */}
              <TabsPanels className="min-h-0 min-w-0 flex-1 grid-cols-[minmax(0,1fr)] grid-rows-[minmax(0,1fr)]">
                {panels.map((p) => (
                  // min-w-0: a wide child (the sensitivity table, a preview card) scrolls inside its page instead of widening
                  // the page past the rail
                  <TabsContent key={p.id} value={p.id} className="min-h-0 min-w-0">
                    <PanelBody panel={p} visible={right.open && p.id === value} fill={p.id === 'drones' || p.id === 'targets' || p.id === 'nests'} />
                  </TabsContent>
                ))}
              </TabsPanels>
            </Tabs>
          </SidebarContent>
        </Sidebar>
      </SidebarProvider>
    </RailHost>
  )
}

export const DroneRailMemo = React.memo(DroneRail)
