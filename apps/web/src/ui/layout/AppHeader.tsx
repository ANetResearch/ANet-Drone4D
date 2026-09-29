// Header (M15-FR-012, FR-037; AWR-14 §4.1, §6.12, §7.7, §7.8): fixed 44 px, solid, above the canvas. Brand lock-up; the
// six menus (Label always inside a Group: Base UI error #31) running the shared actions (same guards and hotkeys as the
// palette); the breadcrumb world > run > focus where the world item switches the view world; the simulation clock
// (SIM T+, C-class text at <= 4 Hz on Tier S, rate and TIME state, HoverCard with wall clock and epoch); the connection
// badge (conn.online, morphing to WifiOff with a red outline when the link is down; WS RTT as C-class text; HoverCard
// with RTT, rates, reconnects and serverInfo); role and seat (viewer "read-only" with Eye, operator, admin; request or
// release control); the alarm centre (the only solid red of the header); the command palette and settings.
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
import { useSelection } from '@/stores/selection'
import { layoutActions } from './layoutState'
import { IconButton } from './IconButton'

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

const simTimeNs = () => timeView.tSimMs * 1e6
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
  return (
    <HoverCard>
      <HoverCardTrigger render={<Badge variant="secondary" className="gap-1 font-mono" data-clock="" />}>
        <Icon icon="tl.clock" />
        <span className="text-muted-foreground">SIM</span>
        <BoundText read={simTimeNs} format={fmt.simTime} stale={timeAgeS} className={cn(!online && 'underline decoration-dashed')} />
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
  const p = perfProbe()
  return (
    <HoverCard>
      <HoverCardTrigger render={<Badge variant="outline" data-conn={conn} className={cn('gap-1', !online && conn !== 'IDLE' && 'border-brand text-brand-text')} />}>
        <StateIcon icon="conn.online" alt={!online} label={t(`conn.${conn}`)} />
        <SwapText value={t(`conn.${conn}`)} />
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

export function AppHeader({ compact }: { compact: boolean }) {
  const t = useT()
  const route = useRoute()
  const worldId = route?.params.id ?? null
  const runId = useConnView((s) => s.runId)
  const primary = useSelection((s) => s.primary)
  const layout = usePrefs((s) => s.layout)
  // menus re-evaluate their guards when the session changes
  useConnView((s) => s.version)
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
          </MenubarContent>
        </MenubarMenu>
        <MenubarMenu>
          <MenubarTrigger>{t('menu.view')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <MenubarCheckboxItem checked={layout.left.open} onCheckedChange={() => layoutActions.toggleLeft()}>
                {t('menu.view.left')}
                <Shortcut combo="mod+KeyB" />
              </MenubarCheckboxItem>
              <MenubarCheckboxItem checked={layout.right.open} onCheckedChange={() => layoutActions.toggleRight()}>
                {t('menu.view.right')}
                <Shortcut combo="Backslash" />
              </MenubarCheckboxItem>
              <MenubarCheckboxItem checked={layout.dock.open} onCheckedChange={() => layoutActions.toggleDock()}>
                {t('menu.view.dock')}
                <Shortcut combo="Backquote" />
              </MenubarCheckboxItem>
              <MenubarCheckboxItem checked={layout.hud.open} onCheckedChange={() => runAction('view.hud')}>
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
          </MenubarContent>
        </MenubarMenu>
        <MenubarMenu>
          <MenubarTrigger>{t('menu.mission')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <MenubarItem onClick={() => layoutActions.openDockTab('mission')}>{t('menu.mission.panel')}</MenubarItem>
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
            </MenubarGroup>
          </MenubarContent>
        </MenubarMenu>
        <MenubarMenu>
          <MenubarTrigger>{t('menu.help')}</MenubarTrigger>
          <MenubarContent>
            <MenubarGroup>
              <MenubarItem onClick={() => overlays.set('help', true)}>{t('menu.help.shortcuts')}<Shortcut combo="shift+Slash" /></MenubarItem>
              <MenubarItem onClick={() => navigate('/bench')}>{t('menu.help.bench')}</MenubarItem>
            </MenubarGroup>
            <MenubarSeparator />
            <MenubarGroup>
              <MenubarItem onClick={() => overlays.set('about', true)}>{t('menu.help.about')}</MenubarItem>
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
              <BreadcrumbPage className="max-w-40 truncate font-mono">{runId ?? t('header.noRun')}</BreadcrumbPage>
            </BreadcrumbItem>
            {primary ? (
              <>
                <BreadcrumbSeparator />
                <BreadcrumbItem><BreadcrumbPage className="font-mono">{primary}</BreadcrumbPage></BreadcrumbItem>
              </>
            ) : null}
          </BreadcrumbList>
        </Breadcrumb>
      )}
      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        <Clock />
        <ConnBadge />
        <RoleBadge />
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
