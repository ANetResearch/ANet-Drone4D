// Bottom Dock (M15-FR-012, FR-014, FR-021; AWR-14 §4.5): the panel area between the two rails above the Timeline bar,
// line Tabs (transitions.dev 16 indicator) over TabsPanels (08 page slide); opened and closed with translateY + opacity
// like the rails (M15 §6.4.1, feedback item 14); height resizable 160 px to 50% of the viewport.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { prefs, usePrefs } from '@/stores/prefs'
import { LAYOUT } from '@/lib/tokens/input.gen'
import { Tabs, TabsContent, TabsList, TabsPanels, TabsTrigger } from '@/ui/components/ui/tabs'
import { Icon } from '@/ui/icons/Icon'
import { PanelBody, usePanels } from '@/ui/panels/PanelHost'
import { RailHost } from './RailHost'

export function BottomDock({ viewportW, viewportH }: { viewportW: number; viewportH: number }) {
  const t = useT()
  // the rail geometry and the Dock fields only: a page change of the right rail re-rendered the Dock and every panel body
  // in it (P4-UI, D1-AC-25)
  const leftOpen = usePrefs((s) => s.layout.left.open)
  const leftW = usePrefs((s) => s.layout.left.width)
  const rightOpen = usePrefs((s) => s.layout.right.open)
  const rightW = usePrefs((s) => s.layout.right.width)
  const dock = usePrefs((s) => s.layout.dock)
  const panels = usePanels('bottom')
  const tab = panels.some((p) => p.id === dock.tab) ? dock.tab : (panels[0]?.id ?? '')
  const G = LAYOUT.gapPx
  const x0 = leftOpen ? G + leftW + G : G
  const x1 = rightOpen ? G + rightW + G : G
  const maxH = Math.max(LAYOUT.dockMinPx, Math.round(viewportH * LAYOUT.dockMaxFrac))
  const style = { '--dock-x0': `${x0}px`, '--dock-x1': `${x1}px` } as React.CSSProperties
  return (
    <div style={style} data-dock-width={viewportW - x0 - x1}>
      <RailHost side="bottom" open={dock.open && panels.length > 0} size={Math.min(dock.height, maxH)} minSize={LAYOUT.dockMinPx} maxSize={maxH}
        onResized={(h) => prefs.setLayout({ dock: { height: h } })} label={t('rail.dock')}>
        <div data-slot="dock" className="flex h-full flex-col gap-2 rounded-lg bg-card p-2 ring-1 ring-foreground/10">
          <Tabs value={tab} onValueChange={(v) => prefs.setLayout({ dock: { tab: String(v) } })} className="min-h-0 flex-1">
            <TabsList variant="line">
              {panels.map((p) => (
                <TabsTrigger key={p.id} value={p.id}>
                  <Icon icon={p.icon} data-icon="inline-start" />
                  {t(p.titleKey)}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsPanels className="min-h-0 flex-1 overflow-auto">
              {panels.map((p) => (
                <TabsContent key={p.id} value={p.id} className="min-h-0">
                  <PanelBody panel={p} visible={dock.open && p.id === tab} />
                </TabsContent>
              ))}
            </TabsPanels>
          </Tabs>
        </div>
      </RailHost>
    </div>
  )
}
