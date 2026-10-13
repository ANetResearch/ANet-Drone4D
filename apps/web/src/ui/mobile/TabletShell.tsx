// Tablet shell, the sand-table commander (R-D2-35, R-D2-36; ADR-122; AWR-04 §15.8.2; AWR-14 §20.26.5; M15-FR-230, FR-233;
// UX-FR-153, UX-FR-155). Owner: M15. 768 <= width < 1280 and short side >= 600.
//   landscape: 48 px top bar (brand, world > session, role, field view, full screen, landscape lock, more), the
//     dockable left panel (entity lists, 320 px) and right panel (detail, 360 px; docked, floating or a 48 px icon bar),
//     the viewport in between (the unobscured rect follows the docked widths), and the 64 px task strip at the bottom
//     (task pills, the four quick commands on the selection, the rate, the alarm count). Virtual sticks (120 px) appear in
//     manual control (ui/manual).
//   portrait: the viewport on top and a non-modal drawer at the bottom with three snap points (25 %, 50 %, 90 %): the task
//     strip on top and the tabs fleet, tasks, targets, detail inside.
// The canvas never unmounts (App.tsx); a rotation only re-lays out, the selection, the camera and the link stay.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { useFormFactor } from '@/lib/formFactor'
import { INPUT } from '@/lib/tokens/input.gen'
import { prefs, prefsStore, usePrefs } from '@/stores/prefs'
import { isCityReplay, useSurface } from '@/app/shell/surface'
import { BrandLockup } from '@/ui/brand'
import { Button } from '@/ui/components/ui/button'
import { BottomDrawer, BottomSheet, DRAWER_SNAPS } from '@/ui/components/ui/bottom-sheet'
import { Popover, PopoverContent, PopoverTrigger } from '@/ui/components/ui/popover'
import { Tabs, TabsContent, TabsList, TabsPanels, TabsTrigger } from '@/ui/components/ui/tabs'
import { Icon } from '@/ui/icons/Icon'
import { FieldControls, FIELD_ICON } from '@/ui/field/FieldToolbar'
import { usePerception } from '@/stores/perception'
import { FullscreenButton, OrientationLockButton } from '@/ui/layout/FullscreenButton'
import { ViewportOverlay } from '@/ui/layout/ViewportOverlay'
import { commitRect } from '@/ui/layout/useUnobscuredSync'
import { AlarmList } from '@/ui/notify/AlarmCenter'
import { BANNER_PX, Banners, useBannerCount } from '@/ui/notify/Banners'
import { useSandboxBannerCount } from '@/ui/sandbox/banners'
import { SandboxDialogsHost } from '@/ui/sandbox/dialogs'
import { Onboarding } from '@/ui/sandbox/Onboarding'
import { entities } from '@/ui/sandbox/entities'
import { ConnDot, safeInsets } from './safeArea'
import { DOCK_PX, DockPanel, dockFit, useDock } from './DockPanel'
import { EntityList, EntityPages, EntitySwitch, ENTS, ENT_ICON, MoreMenu, PanelSlot, RoleChip, useEntity } from './parts'
import { QuickCommandSheet } from './QuickCommand'
import { TaskStrip } from './TaskStrip'
import { SandboxUnavailable } from '@/ui/views/Sandbox'
import { useHeaderHeight } from './safeArea'

export const TABLET_TOP_PX = 48
export const TASK_STRIP_PX = 64

function FieldMenu() {
  const t = useT()
  const view = usePerception((s) => s.fieldView)
  return (
    <Popover>
      <PopoverTrigger render={<Button size="sm" variant="outline" className="h-9 gap-1.5" data-field-menu={view} />}>
        <Icon icon={FIELD_ICON[view]} />
        {t('field.title')}
      </PopoverTrigger>
      <PopoverContent align="end" className="w-auto" data-field-popover="">
        <FieldControls stacked />
      </PopoverContent>
    </Popover>
  )
}

export function TabletTopBar() {
  const t = useT()
  const world = useSurface((s) => s.worldId)
  const surface = useSurface((s) => s.surface)
  const sid = useSurface((s) => s.sid)
  const rtPath = useSurface((s) => s.rtPath)
  // D2.2-FX-world: isCityReplay (app/shell/surface.ts) is the same test AppHeader.tsx and parts.tsx RoleChip use, so
  // the breadcrumb and the role chip right next to it always agree — a city's recorded show used to read "shared
  // demo" here while the chip already said "simulated replay".
  const crumbKey = surface === 'static' ? 'role.static' : isCityReplay(surface, rtPath) ? 'role.cityReplay' : 'mobile.shared'
  return (
    <header data-figure="header" data-mobile-header="tablet" className="mobile-top border-b bg-card">
      <div className="flex h-12 items-center gap-2 px-2">
      <BrandLockup compact />
      <span className="min-w-0 truncate text-hud-sub text-muted-foreground" data-crumb="world">
        {`${world ?? '—'} › ${surface === 'sandbox' ? (sid ?? '') : t(crumbKey)}`}
      </span>
      <ConnDot />
      <div className="ml-auto flex shrink-0 items-center gap-1">
        <RoleChip />
        <FieldMenu />
        <OrientationLockButton />
        <FullscreenButton />
        <MoreMenu />
      </div>
      </div>
    </header>
  )
}

/**
 * the unobscured rect of the tablet layouts (CSS px). A floating side panel covers the viewport like a docked one as far as
 * the overlay anchors and the camera's view centre are concerned (the canvas keeps its size), so the camera tools, the
 * queue cards and the HUD never sit under it; `banner` is the height of the banners under the top bar.
 */
export function tabletRect(w: number, h: number, portrait: boolean, left: string, right: string, drawerSnap: number, insets: { top: number; bottom: number }, banner = 0): { x: number; y: number; w: number; h: number } {
  const top = TABLET_TOP_PX + insets.top + banner
  if (portrait) return { x: 0, y: top, w, h: Math.max(0, h * (1 - drawerSnap) - top) }
  const side = (m: string, px: number): number => (m === 'docked' ? px : m === 'floating' ? px + 8 : m === 'collapsed' ? DOCK_PX.collapsed : 0)
  const x0 = side(left, DOCK_PX.left)
  const x1 = side(right, DOCK_PX.right)
  return { x: x0, y: top, w: Math.max(0, w - x0 - x1), h: Math.max(0, h - top - TASK_STRIP_PX - insets.bottom) }
}

export function TabletShell() {
  const surface = useSurface((s) => s.surface)
  useHeaderHeight('calc(48px + env(safe-area-inset-top))')
  if (surface === 'sandbox-gone') return <SandboxUnavailable />
  return <TabletBody />
}

function TabletBody() {
  const t = useT()
  const ff = useFormFactor()
  const portrait = ff.orient === 'portrait'
  const dockState = useDock((s) => s)
  const ent = useEntity()
  const [snap, setSnap] = React.useState(DRAWER_SNAPS[0] as number)
  const [tab, setTab] = React.useState<'fleet' | 'tasks' | 'targets' | 'detail'>('fleet')
  const [alerts, setAlerts] = React.useState(false)
  const [size, setSize] = React.useState(() => ({ w: globalThis.innerWidth || 1024, h: globalThis.innerHeight || 768 }))
  React.useEffect(() => {
    let tm: ReturnType<typeof setTimeout> | null = null
    const on = (): void => {
      if (tm) clearTimeout(tm)
      tm = setTimeout(() => setSize({ w: innerWidth, h: innerHeight }), INPUT.resizeDebounceMs)
    }
    addEventListener('resize', on)
    return () => {
      removeEventListener('resize', on)
      if (tm) clearTimeout(tm)
    }
  }, [])
  const page = usePrefs((s) => s.layout.right.page)
  const fit = dockFit(dockState, size.w)
  // a detail panel that would float over the viewport with nothing to show folds to its icon bar until an entity opens
  const eff = { left: fit.left, right: fit.right === 'floating' && page === 'list' && dockState.right !== 'floating' ? 'collapsed' as const : fit.right }
  const banner = (useBannerCount() + useSandboxBannerCount()) * BANNER_PX
  React.useEffect(() => {
    commitRect(tabletRect(size.w, size.h, portrait, eff.left, eff.right, snap, safeInsets(), banner), 'breakpoint')
  }, [size.w, size.h, portrait, eff.left, eff.right, snap, banner])
  const top = TABLET_TOP_PX + safeInsets().top
  const bottom = TASK_STRIP_PX + safeInsets().bottom
  const openTasks = (): void => {
    if (portrait) {
      setTab('tasks')
      if (snap < 0.5) setSnap(0.5)
    } else prefs.setLayout({ dock: { open: true, tab: 'tasks' } })
  }
  const listIcons = ENTS.map((e) => ({ icon: ENT_ICON[e], label: t(`rail.ent.${e}`), onClick: () => entities.show(e) }))
  return (
    <div data-view="sandbox" data-shell="tablet" data-orient={ff.orient}>
      <TabletTopBar />
      <ViewportOverlay compactBp variant="tablet" />
      <Banners compact />
      <SandboxDialogsHost />
      <Onboarding />
      <QuickCommandSheet />
      {portrait ? (
        <BottomDrawer snap={snap} onSnap={setSnap} handleLabel={t('mobile.drawer')}
          header={<TaskStrip onTasks={openTasks} onAlerts={() => setAlerts(true)} className="h-auto min-h-14 border-t-0" />}>
          <Tabs value={tab} onValueChange={(v) => setTab(v as typeof tab)} className="min-h-0 flex-1" data-drawer-tabs={tab}>
            <TabsList variant="line" className="w-full">
              <TabsTrigger value="fleet" data-drawer-tab="fleet"><Icon icon="nav.fleet" data-icon="inline-start" />{t('tab.fleet')}</TabsTrigger>
              <TabsTrigger value="tasks" data-drawer-tab="tasks"><Icon icon="tab.tasks" data-icon="inline-start" />{t('tab.tasks')}</TabsTrigger>
              <TabsTrigger value="targets" data-drawer-tab="targets"><Icon icon="target.person" data-icon="inline-start" />{t('tab.targets')}</TabsTrigger>
              <TabsTrigger value="detail" data-drawer-tab="detail"><Icon icon="panel.right" data-icon="inline-start" />{t('tab.detail')}</TabsTrigger>
            </TabsList>
            <TabsPanels className="min-h-0 flex-1">
              <TabsContent value="fleet" className="min-h-0"><EntityPages forced="drones" visible={tab === 'fleet'} /></TabsContent>
              <TabsContent value="tasks" className="min-h-0"><PanelSlot id="tasks" visible={tab === 'tasks'} /></TabsContent>
              <TabsContent value="targets" className="min-h-0"><EntityPages forced="targets" visible={tab === 'targets'} /></TabsContent>
              <TabsContent value="detail" className="min-h-0"><EntityPages emptyWhenList visible={tab === 'detail'} /></TabsContent>
            </TabsPanels>
          </Tabs>
        </BottomDrawer>
      ) : (
        <>
          <DockPanel side="left" mode={eff.left} top={top} bottom={bottom} banner={banner} icons={listIcons}
            title={<EntitySwitch value={ent} size="sm" onChange={(e) => entities.show(e)} />}>
            <EntityList />
          </DockPanel>
          <DockPanel side="right" mode={eff.right} top={top} bottom={bottom} banner={banner} icons={[{ icon: 'panel.right', label: t('tab.detail'), onClick: () => undefined }]}
            title={t('tab.detail')}>
            <EntityPages emptyWhenList />
          </DockPanel>
          <div className="safe-bottom fixed inset-x-0 bottom-0 z-(--z-rails) bg-card">
            <TaskStrip onTasks={openTasks} onAlerts={() => setAlerts(true)} />
          </div>
          <TabletDock />
        </>
      )}
      <BottomSheet open={alerts} onOpenChange={setAlerts} title={t('tab.alerts')} handleLabel={t('tab.alerts')} snaps={[0.5, 0.9]}>
        <AlarmList filter="all" className="max-h-none" />
      </BottomSheet>
    </div>
  )
}

/** the Dock tabs (tasks, relay, events, charts) as a bottom sheet in landscape when opened from the task strip */
function TabletDock() {
  const t = useT()
  const [open, setOpen] = React.useState(false)
  const [tab, setTab] = React.useState('tasks')
  React.useEffect(() => prefsDockListener((p) => {
    if (p.open) {
      setTab(p.tab)
      setOpen(true)
      prefs.setLayout({ dock: { open: false } })
    }
  }), [])
  return (
    <BottomSheet open={open} onOpenChange={setOpen} title={t(`panel.${tab}.title`)} handleLabel={t('tab.tasks')} snaps={[0.5, 0.9]}>
      <PanelSlot id={tab} visible={open} />
    </BottomSheet>
  )
}

/** follow requests to open a Dock tab (menus, actions, the task strip) */
function prefsDockListener(cb: (d: { open: boolean; tab: string }) => void): () => void {
  return prefsStore.subscribe((s, p) => {
    if (s.layout.dock !== p.layout.dock && s.layout.dock.open) cb(s.layout.dock)
  })
}
