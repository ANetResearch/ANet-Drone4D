// Header (M15-FR-012, FR-037; AWR-14 §4.1, §6.12, §7.7, §7.8): fixed 44 px, solid, above the canvas. Brand lock-up; the
// six menus (Label always inside a Group: Base UI error #31) running the shared actions (same guards and hotkeys as the
// palette); the breadcrumb world > run > focus where the world item switches the view world; the simulation clock
// (SIM T+, C-class text at <= 4 Hz on Tier S, rate and TIME state, HoverCard with wall clock and epoch); the connection
// badge (conn.online, morphing to WifiOff with a red outline when the link is down; WS RTT as C-class text; HoverCard
// with RTT, rates, reconnects and serverInfo); role and seat (viewer "read-only" with Eye, operator, admin; request or
// release control); the alarm centre (the only solid red of the header); the command palette and settings.
// Public demo build (ADR-083): the role badge reads "public demo · read-only" with the read-only Tooltip and offers no
// control request; the Jobs, Runs, replay and Bench entries are left out and Help links back to the demo home page.
// D2 (ADR-111; M15-FR-202, FR-205; UX-FR-104, UX-FR-105; AWR-14 §20.2.3, §20.2.4): the role badge follows the run-time
// surface ("public demo · read-only", "static browsing", "my sandbox · operable"); the shared show and static browsing
// offer "open my sandbox"; the World menu gains the sandbox entries (open, switch world, load scenario, export snapshot)
// and Help the open interface page and the onboarding restart.
// D2.1 (ADR-123; AWR-14 §20.26.6; M15-FR-235, FR-236): with the internal identity the role badge reads "internal · full
// access" (identity.shield) and offers "request control" of the shared show (identity token -> run admin token, R01) and
// the way to the identity settings; the Jobs, Runs, replay and Bench entries hidden by the demo build come back.
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { navigate, updateSearch, useRoute } from '@/app/router/router'
import { worldsQuery } from '@/app/query/options'
import { perfProbe } from '@/engine'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/components/ui/badge'
import { Breadcrumb, BreadcrumbItem, BreadcrumbList, BreadcrumbPage, BreadcrumbSeparator } from '@/ui/components/ui/breadcrumb'
import { Button } from '@/ui/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/ui/components/ui/hover-card'
import { Kbd, KbdGroup } from '@/ui/components/ui/kbd'
import { Menubar, MenubarCheckboxItem, MenubarContent, MenubarGroup, MenubarItem, MenubarLabel, MenubarMenu, MenubarSeparator, MenubarShortcut, MenubarTrigger } from '@/ui/components/ui/menubar'
import { BrandLockup } from '@/ui/brand'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import { comboLabel } from '@/ui/hotkeys/registry'
import { getAction, runAction } from '@/ui/actions/registry'
import { BoundText } from '@/ui/motion/BoundText'
import { SwapText } from '@/ui/motion/SwapText'
import { AlarmCenter } from '@/ui/notify/AlarmCenter'
import { overlays } from '@/ui/shell/overlays'
import { useRt } from '@/ui/shell/RtContext'
import { isOnline, roleKeyOf, timeAgeS, timeView, useConnView } from '@/ui/shell/connView'
import { releaseControl, requestControl } from '@/ui/shell/control'
import { usePrefs } from '@/stores/prefs'
import { timelineStore, useTimeline } from '@/stores/timeline'
import { REPO_URL } from '@/ui/views/AboutDialog'
import { useSelection } from '@/stores/selection'
import { DEMO_PUBLIC } from '@/lib/demo'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { isCityReplay, useSurface } from '@/app/shell/surface'
import { sandboxDialogs } from '@/ui/sandbox/dialogs'
import { onboarding } from '@/ui/sandbox/Onboarding'
import { useInternal } from '@/app/shell/surface'
import { identityToken } from '@/stores/identity'
import { requestInternalControl } from '@/ui/shell/control'
import { usePower } from '@/app/power/store'
import { layoutActions } from './layoutState'
import { IconButton } from './IconButton'
import { useWorldScope } from '@/stores/worldScope'

function Shortcut({ combo }: { combo: string }) {
  return <MenubarShortcut>{comboLabel(combo).join(' ')}</MenubarShortcut>
}

/** a menu item bound to an action of the registry (guard, label and hotkey from the action) */
function ActionItem({ id }: { id: string }) {
  const t = useT()
  const a = getAction(id)
  if (!a) return null
  const ok = !a.when || a.when()
  return (
    <MenubarItem disabled={!ok} onClick={() => runAction(id)}>
      {a.icon ? <Icon icon={a.icon} /> : null}
      {t(a.labelKey)}
      {a.hotkey ? <Shortcut combo={a.hotkey} /> : null}
    </MenubarItem>
  )
}

// the seen time of M12 (tDisplayS, the same value as the Timeline readout; M12-to-M15 item 2); unknown before the first TIME
const simTimeNs = () => (timeView.epoch >= 0 || Number.isFinite(timeView.tSimMs) ? timelineStore.getState().tDisplayS * 1e9 : Number.NaN)
const rttMs = () => perfProbe().net.rttMs
const fmtRtt = (v: number) => fmt.ms(v, 0)
const swarmHz = () => perfProbe().net.swarmHz
const selHz = () => perfProbe().net.selectedHz
const fmtHz = (v: number) => `${fmt.num(v, 1)} Hz`

function Clock() {
  const t = useT()
  const state = useConnView((s) => s.timeState)
  const rate = useConnView((s) => s.rate)
  const epoch = useConnView((s) => s.epoch)
  const online = useConnView((s) => isOnline(s.conn))
  // D2.1 (D2-INT, d2-WP-13b-to-M15 item 5): power save (SAVING, RESUMING, QUEUED) closed the link on purpose; the clock
  // keeps the last value without the STALE age (the power overlay gives the time of the last update)
  const frozen = usePower((st) => st.frozen)
  return (
    <HoverCard>
      <HoverCardTrigger render={<Badge variant="secondary" className="gap-1 font-mono" data-clock="" />}>
        <Icon icon="tl.clock" />
        <span className="text-muted-foreground">SIM</span>
        <BoundText read={simTimeNs} format={fmt.simTime} stale={frozen ? undefined : timeAgeS} className={cn(!online && !frozen && 'underline decoration-dashed')} />
        <span>{`×${fmt.num(rate, rate < 1 ? 2 : 0)}`}</span>
        <SwapText value={t(`time.state.${state}`)} className="font-sans" />
      </HoverCardTrigger>
      <HoverCardContent className="w-64 text-hud-sub">
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
          <dt className="text-muted-foreground">{t('clock.sim')}</dt><dd className="font-mono"><BoundText read={simTimeNs} format={fmt.simTime} /></dd>
          <dt className="text-muted-foreground">{t('clock.wall')}</dt><dd className="font-mono"><BoundText read={Date.now} format={fmt.wallTime} /></dd>
          <dt className="text-muted-foreground">{t('clock.rate')}</dt><dd className="font-mono">{`×${fmt.num(rate, 2)}`}</dd>
          <dt className="text-muted-foreground">{t('clock.state')}</dt><dd>{t(`time.state.${state}`)}</dd>
          <dt className="text-muted-foreground">{t('clock.epoch')}</dt><dd className="font-mono">{epoch >= 0 ? fmt.count(epoch) : '—'}</dd>
        </dl>
      </HoverCardContent>
    </HoverCard>
  )
}

function ConnBadge() {
  const t = useT()
  const conn = useConnView((s) => s.conn)
  const attempt = useConnView((s) => s.attempt)
  const world = useConnView((s) => s.sessionWorldId)
  const run = useConnView((s) => s.runId)
  const connId = useConnView((s) => s.connId)
  const online = isOnline(conn)
  // D2.1: the power-save disconnect is intentional: neutral, never the red outline of an outage
  const saving = usePower((st) => st.frozen)
  const p = perfProbe()
  return (
    <HoverCard>
      <HoverCardTrigger render={<Badge variant="outline" data-conn={conn} data-power-saving={saving ? '' : undefined} className={cn('gap-1', !online && conn !== 'IDLE' && !saving && 'border-brand text-brand-text')} />}>
        <StateIcon icon="conn.online" alt={!online} label={t(saving && !online ? 'power.badge' : `conn.${conn}`)} />
        <SwapText value={t(saving && !online ? 'power.badge' : `conn.${conn}`)} />
        {online ? <BoundText read={rttMs} format={fmtRtt} className="text-muted-foreground" /> : null}
      </HoverCardTrigger>
      <HoverCardContent className="w-72 text-hud-sub">
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
          <dt className="text-muted-foreground">{t('connInfo.rtt')}</dt><dd className="font-mono"><BoundText read={rttMs} format={fmtRtt} /></dd>
          <dt className="text-muted-foreground">{t('connInfo.swarmHz')}</dt><dd className="font-mono"><BoundText read={swarmHz} format={fmtHz} /></dd>
          <dt className="text-muted-foreground">{t('connInfo.selHz')}</dt><dd className="font-mono"><BoundText read={selHz} format={fmtHz} /></dd>
          <dt className="text-muted-foreground">{t('connInfo.creditSkips')}</dt><dd className="font-mono">{fmt.count(p.net.creditSkips)}</dd>
          <dt className="text-muted-foreground">{t('connInfo.reconnects')}</dt><dd className="font-mono">{fmt.count(Math.max(p.net.reconnects, attempt))}</dd>
          <dt className="text-muted-foreground">{t('connInfo.world')}</dt><dd className="font-mono">{world ?? '—'}</dd>
          <dt className="text-muted-foreground">{t('connInfo.run')}</dt><dd className="truncate font-mono">{run ?? '—'}</dd>
          <dt className="text-muted-foreground">{t('connInfo.conn')}</dt><dd className="truncate font-mono">{connId ?? '—'}</dd>
        </dl>
      </HoverCardContent>
    </HoverCard>
  )
}

/**
 * the role badge of the D2 surfaces: shared show "public demo · read-only" and static browsing "static browsing" (both
 * with "open my sandbox"), sandbox "my sandbox · operable"; the shared show never offers "request control"
 */
/** D2.1: the internal identity's badge with "request control" of the shared show and the identity settings */
function InternalBadge() {
  const t = useT()
  const rt = useRt()
  const role = useConnView((s) => s.role)
  const seat = useConnView((s) => s.seat)
  const surface = useSurface((s) => s.surface)
  const holds = seat === 'held' && (role === 'admin' || role === 'operator')
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="sm" variant="outline" className="gap-1" data-identity-badge="" data-role={role ?? ''} data-seat={seat ?? ''} />}>
        <Icon icon="identity.shield" data-icon="inline-start" />
        {t('identity.badge')}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t('identity.badge')}</DropdownMenuLabel>
          {surface === 'shared' || surface === 'local' ? (
            holds ? (
              <DropdownMenuItem onClick={() => releaseControl()}><Icon icon="logout" />{t('control.release')}</DropdownMenuItem>
            ) : (
              <DropdownMenuItem data-identity-control="" onClick={() => {
                const tok = identityToken()
                if (tok) void requestInternalControl(rt, tok)
              }}><Icon icon="lease.held" />{t('identity.requestControl')}</DropdownMenuItem>
            )
          ) : null}
          <DropdownMenuItem onClick={() => updateSearch({ settings: 'general' })}><Icon icon="nav.settings" />{t('identity.section')}</DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function SurfaceRoleBadge() {
  const t = useT()
  const role = useConnView((s) => s.role)
  const surface = useSurface((s) => s.surface)
  const world = useSurface((s) => s.worldId)
  const rtPath = useSurface((s) => s.rtPath)
  // D2.2-W2: a city's recorded show is surfaced the same as the live show (surface = 'shared') but labelled
  // honestly as a simulated replay, not the live scenario (isCityReplay: app/shell/surface.ts)
  const replay = isCityReplay(surface, rtPath)
  const internal = useInternal()
  if (internal && surface !== 'sandbox') {
    return (
      <span className="flex items-center gap-1.5">
        <InternalBadge />
        <Button size="sm" variant="outline" onClick={() => sandboxDialogs.open('start', { world: surface === 'static' ? world : null })} data-header-open-sandbox="">
          <Icon icon="sandbox.mine" data-icon="inline-start" />
          {t('landing.sandbox.open')}
        </Button>
      </span>
    )
  }
  if (surface === 'sandbox') {
    return (
      <Badge variant="secondary" className="h-6 gap-1" data-role={role ?? ''} data-demo-role="" data-surface="sandbox">
        <Icon icon="sandbox.mine" />
        {t('role.sandbox')}
      </Badge>
    )
  }
  const stat = surface === 'static'
  const labelKey = stat ? 'role.static' : replay ? 'role.cityReplay' : 'role.shared'
  const hintKey = stat ? 'hint.staticBrowse' : replay ? 'hint.cityReplay' : 'hint.sharedReadOnly'
  const icon = stat ? 'layer.pointcloud' : replay ? 'tl.history' : 'layer.visible'
  return (
    <span className="flex items-center gap-1.5">
      <Tooltip>
        <TooltipTrigger render={<Badge variant="outline" tabIndex={0} className="h-6 gap-1" data-role={role ?? ''} data-demo-role="" data-surface={surface} data-city-replay={replay} />}>
          <Icon icon={icon} />
          {t(labelKey)}
        </TooltipTrigger>
        <TooltipContent>{t(hintKey)}</TooltipContent>
      </Tooltip>
      <Button size="sm" variant="outline" onClick={() => sandboxDialogs.open('start', { world: stat ? world : null })} data-header-open-sandbox="">
        <Icon icon="sandbox.mine" data-icon="inline-start" />
        {t('landing.sandbox.open')}
      </Button>
    </span>
  )
}

function RoleBadge() {
  const t = useT()
  const rt = useRt()
  const role = useConnView((s) => s.role)
  const seat = useConnView((s) => s.seat)
  const online = useConnView((s) => isOnline(s.conn))
  const key = roleKeyOf({ role, seat })
  const writer = key === 'role.operator' || key === 'role.admin'
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="sm" variant="outline" aria-label={t('header.role')} data-role={role ?? ''} data-seat={seat ?? ''} />}>
        <Icon icon={writer ? 'user' : 'layer.visible'} data-icon="inline-start" />
        {t(key)}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t('header.role')}</DropdownMenuLabel>
          {writer ? (
            <DropdownMenuItem disabled={!online} onClick={() => releaseControl()}>
              <Icon icon="logout" />
              {t('control.release')}
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem disabled={!online && rt.status !== 'IDLE'} onClick={() => void requestControl(rt)}>
              <Icon icon="lease.held" />
              {t('control.request')}
            </DropdownMenuItem>
          )}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function WorldCrumb({ worldId }: { worldId: string | null }) {
  const t = useT()
  const worlds = useQuery(worldsQuery())
  const ids = worlds.data?.map((w) => w.id) ?? (worldId ? [worldId] : [])
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" size="xs" className="font-mono" aria-label={t('world.select')} />}>
        {worldId ?? t('header.noWorld')}
        <Icon icon="chev.down" data-icon="inline-end" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t('world.select')}</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={worldId ?? ''} onValueChange={(v) => v && navigate(`/world/${String(v)}`)}>
            {ids.map((id) => <DropdownMenuRadioItem key={id} value={id} className="font-mono">{id}</DropdownMenuRadioItem>)}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem onClick={() => navigate('/worlds')}>
            <Icon icon="nav.world" />
            {t('menu.world.open')}
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * the focus vehicle as the current page of the breadcrumb trail; clicking it focuses the camera (AWR-14 §2.3). Its own
 * component, so a selection change re-renders this crumb, not the whole header with its menubar inside the key handler
 * (ADR-069; D1-AC-27)
 */
function FocusCrumb() {
  const primary = useSelection((s) => s.primary)
  if (!primary) return null
  return (
    <>
      <BreadcrumbSeparator />
      <BreadcrumbItem>
        <Button variant="ghost" size="xs" className="font-mono text-foreground" aria-current="page" data-crumb="focus"
          onClick={() => runAction('camera.focus')}>
          {primary}
        </Button>
      </BreadcrumbItem>
    </>
  )
}

/** World menu entries of the sandbox (AWR-14 §20.2.4): open (shared, static), switch world, load scenario, export (op) */
function SandboxMenuItems() {
  const t = useT()
  const surface = useSurface((s) => s.surface)
  const world = useSurface((s) => s.worldId)
  if (surface === 'local') return null
  return (
    <>
      <MenubarSeparator />
      <MenubarGroup>
        {surface === 'sandbox' ? (
          <>
            <MenubarItem onClick={() => sandboxDialogs.open('world')}>{t('sandbox.bar.world')}</MenubarItem>
            <MenubarItem onClick={() => sandboxDialogs.open('template')}>{t('sandbox.bar.template')}</MenubarItem>
            <MenubarItem onClick={() => void sandboxDialogs.downloadSnapshot()}>{t('sandbox.exportSnapshot')}</MenubarItem>
            <MenubarItem onClick={() => sandboxDialogs.open('pair')} data-menu-pair=""><Icon icon="device.pair" />{t('pair.title')}</MenubarItem>
          </>
        ) : (
          <MenubarItem onClick={() => sandboxDialogs.open('start', { world: surface === 'static' ? world : null })}>{t('landing.sandbox.open')}</MenubarItem>
        )}
      </MenubarGroup>
    </>
  )
}

export function AppHeader({ compact }: { compact: boolean }) {
  const t = useT()
  const route = useRoute()
  const surface = useSurface((s) => s.surface)
  const sessionWorld = useConnView((s) => s.sessionWorldId)
  // overlay pages (World Hub, Runs, Jobs) keep the sandbox's world in the breadcrumb; the sandbox route shows the
  // session's world (its route parameter is the sid)
  const surfaceWorld = useSurface((s) => s.worldId)
  const worldId = (route?.route.id === 'sandbox' ? surfaceWorld : route?.params.id) ?? sessionWorld ?? null
  const runId = useConnView((s) => s.runId)
  // the four panel switches of the View menu, not the whole layout: a rail page change or a resize (row click -> detail
  // page) no longer re-renders the header and its six menus (P4-UI, D1-AC-25)
  const leftOpen = usePrefs((s) => s.layout.left.open)
  const rightOpen = usePrefs((s) => s.layout.right.open)
  const dockOpen = usePrefs((s) => s.layout.dock.open)
  const hudOpen = usePrefs((s) => s.layout.hud.open)
  // D2 surfaces and runs with the tasking capability use the task and relay tabs instead of the D1 mission tab
  const tasking = useConnView((s) => surface !== 'local' || s.caps.includes('tasking'))
  // menus re-evaluate their guards when the session changes
  useConnView((s) => s.version)
  const replay = useTimeline((s) => s.mode === 'replay')
  const replayRun = useTimeline((s) => (s.mode === 'replay' ? s.runId : null))
  // D2.1: the internal identity brings back the entries the demo build hides
  const internalOn = useInternal()
  const hidden = DEMO_PUBLIC && !internalOn
  const internalLocal = internalOn && surface === 'local'
  // D2.2-W1: the run of a source that simulates another world (or of no source: static browsing) is not this world's run
  const foreign = useWorldScope((s) => s.bound && s.foreign)
  const shownRun = replayRun ?? (foreign ? null : runId)
  return (
    <header data-figure="header" className="app-layer-header flex items-center gap-2 border-b bg-card px-2">
      <BrandLockup compact={compact} />
      <Menubar className="h-7 border-0 p-0">
        <MenubarMenu>
          <MenubarTrigger>{t('menu.world')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <MenubarLabel>{t('menu.world')}</MenubarLabel>
              <MenubarItem onClick={() => navigate('/worlds')}>{t('menu.world.open')}</MenubarItem>
            </MenubarGroup>
            <SandboxMenuItems />
          </MenubarContent>
        </MenubarMenu>
        <MenubarMenu>
          <MenubarTrigger>{t('menu.view')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <MenubarCheckboxItem checked={leftOpen} onCheckedChange={() => layoutActions.toggleLeft()}>
                {t('menu.view.left')}
                <Shortcut combo="mod+KeyB" />
              </MenubarCheckboxItem>
              <MenubarCheckboxItem checked={rightOpen} onCheckedChange={() => layoutActions.toggleRight()}>
                {t('menu.view.right')}
                <Shortcut combo="Backslash" />
              </MenubarCheckboxItem>
              <MenubarCheckboxItem checked={dockOpen} onCheckedChange={() => layoutActions.toggleDock()}>
                {t('menu.view.dock')}
                <Shortcut combo="Backquote" />
              </MenubarCheckboxItem>
              <MenubarCheckboxItem checked={hudOpen} onCheckedChange={() => runAction('view.hud')}>
                {t('menu.view.hud')}
                <Shortcut combo="KeyP" />
              </MenubarCheckboxItem>
            </MenubarGroup>
          </MenubarContent>
        </MenubarMenu>
        <MenubarMenu>
          <MenubarTrigger>{t('menu.sim')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <ActionItem id="sim.toggle" />
              <ActionItem id="sim.slower" />
              <ActionItem id="sim.faster" />
              <ActionItem id="sim.step" />
            </MenubarGroup>
            <MenubarSeparator />
            <MenubarGroup>
              <ActionItem id="vehicle.add" />
              <ActionItem id="vehicle.remove" />
            </MenubarGroup>
            <MenubarSeparator />
            <MenubarGroup>
              {replay ? <ActionItem id="replay.exit" /> : hidden ? null : <MenubarItem onClick={() => navigate('/runs')}><Icon icon="tl.history" />{t('menu.sim.replay')}</MenubarItem>}
              <ActionItem id="timeline.bookmark" />
            </MenubarGroup>
          </MenubarContent>
        </MenubarMenu>
        <MenubarMenu>
          <MenubarTrigger>{t('menu.mission')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <MenubarItem onClick={() => layoutActions.openDockTab(tasking ? 'tasks' : 'mission')}>{t('menu.mission.panel')}</MenubarItem>
              {tasking ? <ActionItem id="task.new" /> : null}
              {tasking ? <ActionItem id="panel.relay" /> : null}
              <ActionItem id="cmd.goto" />
              <ActionItem id="cmd.hover" />
              <ActionItem id="cmd.rtl" />
              <ActionItem id="cmd.land" />
              <ActionItem id="cmd.safety_stop" />
            </MenubarGroup>
          </MenubarContent>
        </MenubarMenu>
        <MenubarMenu>
          <MenubarTrigger>{t('menu.tools')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <MenubarItem onClick={() => layoutActions.openDockTab('perf')}>{t('menu.tools.perf')}</MenubarItem>
              <MenubarItem onClick={() => layoutActions.openDockTab('events')}>{t('menu.tools.events')}</MenubarItem>
              <MenubarItem onClick={() => layoutActions.openDockTab('charts')}>{t('panel.charts.title')}</MenubarItem>
              <MenubarItem onClick={() => layoutActions.openDockTab('timeline')}>{t('panel.timeline.title')}</MenubarItem>
              <ActionItem id="tool.gsd" />
            </MenubarGroup>
            {hidden ? null : (
              <>
                <MenubarSeparator />
                <MenubarGroup>
                  <MenubarItem onClick={() => navigate('/jobs')} data-menu-jobs=""><Icon icon="nav.recon" />{t('menu.tools.jobs')}</MenubarItem>
                  <MenubarItem onClick={() => navigate('/runs')}><Icon icon="data.folder" />{t('menu.tools.runs')}</MenubarItem>
                </MenubarGroup>
              </>
            )}
          </MenubarContent>
        </MenubarMenu>
        <MenubarMenu>
          <MenubarTrigger>{t('menu.help')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <MenubarItem onClick={() => overlays.set('help', true)}>{t('menu.help.shortcuts')}<Shortcut combo="shift+Slash" /></MenubarItem>
              <MenubarItem onClick={() => navigate('/sandbox/api')} data-menu-api=""><Icon icon="api.doc" />{t('menu.help.api')}</MenubarItem>
              {surface === 'sandbox' ? <MenubarItem onClick={() => onboarding.start(true)}>{t('menu.help.onboarding')}</MenubarItem> : null}
              {DEMO_PUBLIC ? (
                <MenubarItem onClick={() => location.assign('/')} data-demo-home=""><Icon icon="nav.world" />{t('demo.home')}</MenubarItem>
              ) : null}
              {hidden ? null : <MenubarItem onClick={() => navigate('/bench')}>{t('menu.help.bench')}</MenubarItem>}
            </MenubarGroup>
            <MenubarSeparator />
            <MenubarGroup>
              <MenubarItem onClick={() => window.open(REPO_URL, '_blank', 'noopener,noreferrer')}><Icon icon="external" />{t('menu.help.repo')}</MenubarItem>
              <MenubarItem onClick={() => overlays.set('about', true)}><Icon icon="help" />{t('menu.help.about')}</MenubarItem>
            </MenubarGroup>
          </MenubarContent>
        </MenubarMenu>
      </Menubar>
      {compact ? null : (
        <Breadcrumb className="min-w-0">
          <BreadcrumbList className="flex-nowrap">
            <BreadcrumbItem><WorldCrumb worldId={worldId} /></BreadcrumbItem>
            <BreadcrumbSeparator />
            <BreadcrumbItem className="min-w-0">
              {/* the run being viewed (the replayed recording in replay); opens Runs (AWR-14 §2.3, UX-FR-006) */}
              {shownRun && DEMO_PUBLIC ? (
                <BreadcrumbPage className="max-w-44 truncate px-2 font-mono text-xs text-muted-foreground" data-crumb="run">{shownRun}</BreadcrumbPage>
              ) : shownRun ? (
                <Button variant="ghost" size="xs" className="max-w-44 font-mono" onClick={() => navigate('/runs')}
                  aria-label={`${t('runs.open')} ${shownRun}`} data-crumb="run">
                  <span className="truncate">{shownRun}</span>
                </Button>
              ) : (
                <BreadcrumbPage className="px-2 font-mono text-xs text-muted-foreground">{t('header.noRun')}</BreadcrumbPage>
              )}
            </BreadcrumbItem>
            <FocusCrumb />
          </BreadcrumbList>
        </Breadcrumb>
      )}
      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        <Clock />
        <ConnBadge />
        {DEMO_PUBLIC || surface === 'sandbox' ? <SurfaceRoleBadge /> : internalLocal ? <InternalBadge /> : <RoleBadge />}
        <AlarmCenter />
        <Button variant="outline" size="sm" onClick={() => overlays.set('palette', true)} aria-label={t('header.search')}>
          <Icon icon="cmd.search" data-icon="inline-start" />
          {compact ? null : t('header.search')}
          <KbdGroup>
            {comboLabel('mod+KeyK').map((k) => <Kbd key={k}>{k}</Kbd>)}
          </KbdGroup>
        </Button>
        <IconButton icon="nav.settings" label={t('header.settings')} onClick={() => updateSearch({ settings: 'general' })} />
      </div>
    </header>
  )
}

export const AppHeaderMemo = React.memo(AppHeader)
