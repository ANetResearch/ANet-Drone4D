// Left rail WORLD / LAYERS / ENVIRONMENT (M15-FR-012, FR-021; AWR-14 §4.2): a floating offcanvas Sidebar in its RailHost;
// every registered left-slot panel is a Collapsible group (transitions.dev 21). Controlled `open` from awr.ui.layout.v1.
import { useT } from '@/app/i18n'
import { prefs, usePrefs } from '@/stores/prefs'
import { LAYOUT } from '@/lib/tokens/input.gen'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/ui/components/ui/collapsible'
import { Sidebar, SidebarContent, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarProvider } from '@/ui/components/ui/sidebar'
import { Icon } from '@/ui/icons/Icon'
import { PanelBody, usePanels } from '@/ui/panels/PanelHost'
import { RailHost } from './RailHost'

export function WorldSidebar() {
  const t = useT()
  const left = usePrefs((s) => s.layout.left)
  const panels = usePanels('left')
  return (
    <RailHost side="left" open={left.open && panels.length > 0} size={left.width} minSize={LAYOUT.leftMinPx} maxSize={LAYOUT.leftMaxPx}
      onResized={(w) => prefs.setLayout({ left: { width: w } })} label={t('rail.left')}>
      <SidebarProvider open={left.open} onOpenChange={(open) => prefs.setLayout({ left: { open } })} className="h-full min-h-0">
        <Sidebar side="left" variant="floating" collapsible="offcanvas">
          <SidebarContent className="gap-0">
            {panels.map((p) => (
              <Collapsible key={p.id} defaultOpen>
                <SidebarGroup>
                  <SidebarGroupLabel render={<CollapsibleTrigger />} className="w-full gap-1.5 text-hud-cap font-semibold uppercase">
                    <Icon icon={p.icon} />
                    {t(p.titleKey)}
                    <Icon icon="chev.down" className="ml-auto" />
                  </SidebarGroupLabel>
                  <CollapsibleContent>
                    <SidebarGroupContent className="px-2 pb-2">
                      <PanelBody panel={p} visible={left.open} />
                    </SidebarGroupContent>
                  </CollapsibleContent>
                </SidebarGroup>
              </Collapsible>
            ))}
          </SidebarContent>
        </Sidebar>
      </SidebarProvider>
    </RailHost>
  )
}
