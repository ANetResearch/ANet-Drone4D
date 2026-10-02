// Right rail DRONES (M15-FR-012, FR-021, FR-024; AWR-14 §4.4): floating offcanvas Sidebar in its RailHost with a page
// stack (Tabs without a visible list, TabsPanels single-cell grid, transitions.dev 08): the fleet list and the vehicle
// detail. Right-slot panels are the pages.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { prefs, usePrefs } from '@/stores/prefs'
import { LAYOUT } from '@/lib/tokens/input.gen'
import { Sidebar, SidebarContent, SidebarHeader, SidebarProvider } from '@/ui/components/ui/sidebar'
import { Tabs, TabsContent, TabsPanels } from '@/ui/components/ui/tabs'
import { Icon } from '@/ui/icons/Icon'
import { PanelBody, usePanels } from '@/ui/panels/PanelHost'
import { RailHost } from './RailHost'

export function DroneRail() {
  const t = useT()
  const right = usePrefs((s) => s.layout.right)
  const panels = usePanels('right')
  const page = panels.some((p) => p.id === (right.page === 'detail' ? 'drone-detail' : 'drones')) ? right.page : 'list'
  const value = page === 'detail' ? 'drone-detail' : (panels[0]?.id ?? 'drones')
  const current = panels.find((p) => p.id === value)
  return (
    <RailHost side="right" open={right.open && panels.length > 0} size={right.width} minSize={LAYOUT.rightMinPx} maxSize={LAYOUT.rightMaxPx}
      onResized={(w) => prefs.setLayout({ right: { width: w } })} label={t('rail.right')}>
      <SidebarProvider open={right.open} onOpenChange={(open) => prefs.setLayout({ right: { open } })} className="h-full min-h-0">
        <Sidebar side="right" variant="floating" collapsible="offcanvas">
          <SidebarHeader className="flex-row items-center gap-1.5 px-3 pt-3 text-hud-cap font-semibold uppercase text-muted-foreground">
            {current ? <Icon icon={current.icon} /> : null}
            {current ? t(current.titleKey) : null}
          </SidebarHeader>
          <SidebarContent className="px-2 pb-2">
            <Tabs value={value} onValueChange={(v) => prefs.setLayout({ right: { page: v === 'drone-detail' ? 'detail' : 'list' } })} className="min-h-0 flex-1">
              {/* one row of the cell height: the fleet list scrolls inside its own virtualised viewport (rendered rows <=
                  visible + overscan, M15-FR-024, D1-AC-27); without it the cell grew with the rows and every row rendered */}
              <TabsPanels className="min-h-0 flex-1 grid-rows-[minmax(0,1fr)]">
                {panels.map((p) => (
                  <TabsContent key={p.id} value={p.id} className="min-h-0">
                    <PanelBody panel={p} visible={right.open && p.id === value} fill={p.id === 'drones'} />
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
