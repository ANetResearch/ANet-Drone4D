// Phone shell, the simplified commander (R-D2-35, R-D2-37; ADR-122; AWR-04 §15.8.3; AWR-14 §20.26.5; M15-FR-231 to FR-233;
// UX-FR-153, UX-FR-154). Owner: M15. Width < 768 or short side < 600.
//   * 36 px status bar (world, role, the sandbox's remaining time or "paused", the link, more);
//   * one window at a time: viewport (the canvas with the camera toolbar, the selected-vehicle chip, the "layers" drawer
//     with the field view and the volumes, and the "commands" button of the sandbox operator), fleet (vehicle list ->
//     detail), tasks, targets (list -> detail with the perception readout), alerts (acknowledge); switching keeps every
//     window's scroll and selection (hidden windows stay mounted); leaving the viewport window suspends the canvas (it is
//     covered), coming back resumes it;
//   * the tab bar at the bottom (portrait) or on the left (landscape);
//   * no virtual sticks on phones (quick commands only, AWR-04 §15.8.3 item 4); viewers switch windows and read only.
// NarrowShell: below 360 CSS px the "window too narrow" Empty replaces the shell and the viewport is suspended.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useFormFactor } from '@/lib/formFactor'
import { prefs, prefsStore, usePrefs } from '@/stores/prefs'
import { useSelection } from '@/stores/selection'
import { useSurface } from '@/app/shell/surface'
import { usePower } from '@/app/power/PowerController'
import { FullEmptyState } from '@/ui/brand'
import { Badge } from '@/ui/components/ui/badge'
import { BottomSheet } from '@/ui/components/ui/bottom-sheet'
import { Button } from '@/ui/components/ui/button'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { FieldControls, FieldLegend } from '@/ui/field/FieldToolbar'
import { CameraModes, ViewportOverlay, ViewTools } from '@/ui/layout/ViewportOverlay'
import { commitRect } from '@/ui/layout/useUnobscuredSync'
import { AlarmList, type AlarmFilter } from '@/ui/notify/AlarmCenter'
import { alarms } from '@/ui/notify/alarms'
import { BANNER_PX, Banners, useBannerCount } from '@/ui/notify/Banners'
import { useSandboxBannerCount } from '@/ui/sandbox/banners'
import { PausedNote } from '@/ui/power/PowerOverlay'
import { SandboxDialogsHost } from '@/ui/sandbox/dialogs'
import { Onboarding } from '@/ui/sandbox/Onboarding'
import { remainingS, useSandbox } from '@/ui/sandbox/session'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { frameCap } from '@/ui/shell/frameCap'
import { UX } from '@/ui/testing/uxProbe'
import { ConnDot, safeInsets } from './safeArea'
import { EntityPages, MoreMenu, PanelSlot, RoleChip } from './parts'
import { QuickCommandSheet, quick } from './QuickCommand'
import { TAB_BAR_PX, TAB_RAIL_PX, TabBar, type PhoneTab } from './TabBar'
import { SandboxUnavailable } from '@/ui/views/Sandbox'
import { useHeaderHeight } from './safeArea'

export const STATUS_BAR_PX = 36

/** the unobscured rect of the phone viewport window (CSS px) */
export function phoneRect(w: number, h: number, landscape: boolean, insets: { top: number; bottom: number; left: number }, banner = 0): { x: number; y: number; w: number; h: number } {
  const top = STATUS_BAR_PX + insets.top + banner
  if (landscape) {
    const x = TAB_RAIL_PX + insets.left
    return { x, y: top, w: Math.max(0, w - x), h: Math.max(0, h - top - insets.bottom) }
  }
  return { x: 0, y: top, w, h: Math.max(0, h - top - TAB_BAR_PX - insets.bottom) }
}

function StatusBar() {
  const world = useSurface((s) => s.worldId)
  const surface = useSurface((s) => s.surface)
  const sess = useSandbox((s) => (surface === 'sandbox' ? s.session : null))
  const frozen = usePower((s) => s.frozen)
  const [, tick] = React.useReducer((x: number) => x + 1, 0)
  React.useEffect(() => {
    if (!sess || frozen) return
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [sess, frozen])
  const left = sess ? remainingS({ session: sess }) : Number.NaN
  return (
    <header data-figure="header" data-mobile-header="phone" className="mobile-top border-b bg-card">
      <div className="flex h-9 items-center gap-1.5 px-2 text-hud-sub">
        <span className="min-w-0 truncate font-medium" data-status-world="">{world ?? '—'}</span>
        <RoleChip compact />
        {frozen ? <PausedNote className="h-6" /> : Number.isFinite(left) ? (
          <Badge variant="outline" className="h-6 font-mono tabular-nums" data-status-remaining="">{fmt.dur(left)}</Badge>
        ) : null}
        <ConnDot className="ml-auto" />
        <MoreMenu />
      </div>
    </header>
  )
}

function SelectedChip() {
  const primary = useSelection((s) => s.primary)
  const n = useSelection((s) => s.ids.length)
  if (!primary) return null
  return (
    <Badge variant="secondary" className="h-8 gap-1 px-2.5 font-mono" data-selected-chip={primary}>
      <Icon icon="drone.quad" />
      {primary}
      {n > 1 ? <span className="text-muted-foreground">{` +${n - 1}`}</span> : null}
    </Badge>
  )
}

/** the phone viewport window's own controls (bottom right): layers drawer and the commands button */
function ViewportControls() {
  const t = useT()
  const write = useD2Write()
  const primary = useSelection((s) => s.primary)
  const [layers, setLayers] = React.useState(false)
  return (
    <>
      <div data-anchor="bottom-left" className="pointer-events-auto"><SelectedChip /></div>
      <div data-anchor="bottom-right" className="pointer-events-auto flex flex-col items-end gap-2" data-phone-controls="">
        <Button size="icon-lg" variant="secondary" className="size-12 rounded-full shadow-md" aria-label={t('field.layers')} onClick={() => setLayers(true)} data-phone-layers="">
          <Icon icon="field.layers" />
        </Button>
        {write ? (
          <Button size="lg" className="h-12 gap-1.5 rounded-full px-4 shadow-md" onClick={() => quick.open()} data-phone-command="">
            <Icon icon="cmd.quick" />
            {t('cmd.quick.title')}
          </Button>
        ) : null}
      </div>
      <BottomSheet open={layers} onOpenChange={setLayers} title={t('field.layers')} handleLabel={t('field.layers')} snaps={[0.5, 0.9]}>
        <div className="flex flex-col gap-3" data-layers-sheet="">
          <div className="flex flex-col items-start gap-1.5" data-phone-camera="">
            <span className="text-hud-cap text-muted-foreground">{t('camera.modes')}</span>
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex items-center gap-1 rounded-lg bg-hud p-1 ring-1 ring-foreground/10"><CameraModes primary={primary} /></span>
              <ViewTools orientation="horizontal" />
            </div>
          </div>
          <FieldControls stacked />
          <FieldLegendInline />
        </div>
      </BottomSheet>
    </>
  )
}

/** the legend inside the drawer (not anchored to the viewport) */
function FieldLegendInline() {
  return <FieldLegend inline />
}

function AlertsWindow() {
  const t = useT()
  const [filter, setFilter] = React.useState<AlarmFilter>('all')
  return (
    <div className="flex min-h-0 flex-col gap-2" data-window-alerts="">
      <div className="flex items-center justify-between gap-2">
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[filter]} aria-label={t('alarm.filter')}
          onValueChange={(v: unknown[]) => v[0] !== undefined && setFilter(v[0] as AlarmFilter)}>
          <ToggleGroupItem value="all">{t('alarm.filter.all')}</ToggleGroupItem>
          <ToggleGroupItem value="critical">{t('alarm.filter.critical')}</ToggleGroupItem>
          <ToggleGroupItem value="warning">{t('alarm.filter.warning')}</ToggleGroupItem>
        </ToggleGroup>
        <Button size="sm" variant="ghost" onClick={() => alarms.ackAll()}>{t('alarm.ackAll')}</Button>
      </div>
      <AlarmList filter={filter} className="max-h-none" />
    </div>
  )
}

function Window({ id, active, pad, children }: { id: PhoneTab; active: boolean; pad?: React.CSSProperties; children: React.ReactNode }) {
  return (
    <section data-window={id} data-active={active ? '' : undefined} aria-hidden={active ? undefined : true} inert={!active} style={pad}
      className={cn('phone-window absolute inset-0 overflow-y-auto bg-background p-2', !active && 'invisible pointer-events-none')}>
      {children}
    </section>
  )
}

export function PhoneShell() {
  const surface = useSurface((s) => s.surface)
  useHeaderHeight('calc(36px + env(safe-area-inset-top))')
  if (surface === 'sandbox-gone') return <SandboxUnavailable />
  return <PhoneBody />
}

function PhoneBody() {
  const ff = useFormFactor()
  const landscape = ff.orient === 'landscape'
  const tab = usePrefs((s) => s.ui.mobileTab)
  const setTab = (x: PhoneTab): void => {
    // fleet and targets share the right-column page state: a detail page of the other entity opens as its list (D2-INT,
    // d2-WP-13b-to-M15 item 7: the targets window used to open on an empty "no target selected" detail page)
    const cur = prefsStore.getState().layout.right
    const open = (ent: 'drones' | 'targets'): void => prefs.setLayout({ right: cur.ent === ent ? { ent } : { ent, page: 'list' } })
    if (x === 'fleet') open('drones')
    if (x === 'targets') open('targets')
    prefs.setUi('mobileTab', x)
  }
  const [size, setSize] = React.useState(() => ({ w: globalThis.innerWidth || 390, h: globalThis.innerHeight || 844 }))
  React.useEffect(() => {
    const on = (): void => setSize({ w: innerWidth, h: innerHeight })
    addEventListener('resize', on)
    return () => removeEventListener('resize', on)
  }, [])
  const banner = (useBannerCount() + useSandboxBannerCount(false)) * BANNER_PX
  React.useEffect(() => {
    commitRect(phoneRect(size.w, size.h, landscape, safeInsets(), banner), 'breakpoint')
  }, [size.w, size.h, landscape, banner])
  // the canvas is covered by the other windows: suspend it there, resume on the viewport window (AWR-04 §15.8.3 item 1)
  React.useEffect(() => {
    frameCap.suspend('phone-window', tab !== 'viewport')
    return () => frameCap.suspend('phone-window', false)
  }, [tab])
  React.useEffect(() => {
    UX.layout.breakpoint = 'phone'
  }, [])
  const insets = safeInsets()
  // the windows reach the bottom edge (opaque, the tab bar lies on top) and keep their last rows clear of the tab bar
  const frame = landscape
    ? { top: STATUS_BAR_PX + insets.top, left: TAB_RAIL_PX + insets.left, right: 0, bottom: 0 }
    : { top: STATUS_BAR_PX + insets.top, left: 0, right: 0, bottom: 0 }
  const windowPad = landscape ? undefined : { paddingBottom: TAB_BAR_PX + insets.bottom + 16 }
  return (
    <div data-view="sandbox" data-shell="phone" data-orient={ff.orient} data-tab={tab}>
      <StatusBar />
      <ViewportOverlay compactBp variant="phone" />
      {tab === 'viewport' ? <ViewportControls /> : null}
      <Banners compact session={false} />
      <SandboxDialogsHost />
      <Onboarding />
      <QuickCommandSheet />
      <div className={cn('fixed z-(--z-rails)', tab === 'viewport' && 'pointer-events-none')} style={frame} data-windows="">
        <Window id="fleet" active={tab === 'fleet'} pad={windowPad}><EntityPages forced="drones" visible={tab === 'fleet'} /></Window>
        <Window id="tasks" active={tab === 'tasks'} pad={windowPad}><PanelSlot id="tasks" visible={tab === 'tasks'} /></Window>
        <Window id="targets" active={tab === 'targets'} pad={windowPad}><EntityPages forced="targets" visible={tab === 'targets'} /></Window>
        <Window id="alerts" active={tab === 'alerts'} pad={windowPad}><AlertsWindow /></Window>
      </div>
      <TabBar value={tab} onChange={setTab} vertical={landscape} />
    </div>
  )
}

export function NarrowShell() {
  const t = useT()
  React.useEffect(() => {
    frameCap.suspend('narrow', true)
    return () => frameCap.suspend('narrow', false)
  }, [])
  return <FullEmptyState title={t('ff.narrow')} description={t('ff.narrowHint')} narrow />
}
