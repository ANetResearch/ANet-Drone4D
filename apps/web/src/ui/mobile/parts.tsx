// Shared parts of the phone and tablet shells (R-D2-35 to R-D2-37; ADR-122; AWR-04 §15.8; AWR-14 §20.26.5; M15-FR-229 to
// FR-233). Owner: M15. Panels are the registered ones (ui/panels/registry): the shells only place them.
//   * PanelSlot: one registered panel by id with its error boundary and visibility wiring;
//   * EntitySwitch, EntityList, EntityPages: the drones / targets / nests lists and their detail pages from the same
//     page state as the desktop rail (awr.ui.layout.v1 right.ent and right.page);
//   * RoleChip: the role of this page (public demo read-only, static browsing, my sandbox, internal full access);
//   * MoreMenu: settings, about, open my sandbox, the open interface, the onboarding;
//   * LongPressMenu: the canvas long press (M06 touch.onLongPress) opens a DropdownMenu anchored at the touch point: fly
//     here (the selected vehicle), place a target here (sandbox operator), measure, copy coordinates.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate, updateSearch } from '@/app/router/router'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { copyText } from '@/lib/build'
import { rtClient } from '@/net/rt'
import { usePrefs } from '@/stores/prefs'
import { selectionStore } from '@/stores/selection'
import { targetsActions } from '@/stores/targets'
import { isCityReplay, useSurface } from '@/app/shell/surface'
import { useIdentity } from '@/stores/identity'
import { touch } from '@/viewport/facade'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { PanelBody } from '@/ui/panels/PanelHost'
import { getPanel, panelsSnapshot, subscribePanels } from '@/ui/panels/registry'
import { pagePanel } from '@/ui/layout/DroneRail'
import { entities, type Entity } from '@/ui/sandbox/entities'
import { sandboxDialogs } from '@/ui/sandbox/dialogs'
import { sandbox } from '@/ui/sandbox/session'
import { confirmThen } from '@/ui/actions/ConfirmHost'
import { onboarding } from '@/ui/sandbox/Onboarding'
import { overlays } from '@/ui/shell/overlays'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { gotoAltitude } from './quickModel'

export const ENT_ICON: Readonly<Record<Entity, IconKey>> = { drones: 'drone.quad', targets: 'target.person', nests: 'nest.box' }
export const ENTS: readonly Entity[] = ['drones', 'targets', 'nests']

/** one registered panel by id (null until the registry has it) */
export function PanelSlot({ id, visible = true, fill = false }: { id: string; visible?: boolean; fill?: boolean }) {
  const snap = React.useSyncExternalStore(subscribePanels, panelsSnapshot, panelsSnapshot)
  const p = React.useMemo(() => (snap ? getPanel(id) : undefined), [snap, id])
  if (!p) return null
  return <PanelBody panel={p} visible={visible} fill={fill} />
}

export function EntitySwitch({ value, onChange, size = 'default' }: { value: Entity; onChange: (e: Entity) => void; size?: 'sm' | 'default' }) {
  const t = useT()
  return (
    <ToggleGroup spacing={0} size={size} value={[value]} aria-label={t('rail.entities')} data-entity-switch={value}
      onValueChange={(v: unknown[]) => ENTS.includes(v[0] as Entity) && onChange(v[0] as Entity)}>
      {ENTS.map((e) => (
        <ToggleGroupItem key={e} value={e} data-entity={e}>
          <Icon icon={ENT_ICON[e]} data-icon="inline-start" />
          {t(`rail.ent.${e}`)}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/** the current entity of the page state (`right.ent`) */
export function useEntity(): Entity {
  return usePrefs((s) => s.layout.right.ent ?? 'drones')
}

/** the list panel of an entity (tablet left dock) */
export function EntityList({ visible = true }: { visible?: boolean }) {
  const ent = useEntity()
  return <PanelSlot id={pagePanel('list', ent)} visible={visible} fill />
}

/** the detail, editor or new-task page of the page state; `emptyWhenList` shows nothing on the list page (tablet right dock) */
export function EntityPages({ visible = true, emptyWhenList = false, forced }: { visible?: boolean; emptyWhenList?: boolean; forced?: Entity }) {
  const t = useT()
  const right = usePrefs((s) => s.layout.right)
  const ent = forced ?? right.ent ?? 'drones'
  const page = forced && right.ent !== forced ? 'list' : right.page
  if (emptyWhenList && page === 'list') {
    return <p className="p-3 text-hud-sub text-muted-foreground" data-dock-empty="">{t('dock.pickEntity')}</p>
  }
  return <PanelSlot id={pagePanel(page, ent)} visible={visible} fill={page === 'list'} />
}

export function RoleChip({ compact = false }: { compact?: boolean }) {
  const t = useT()
  const surface = useSurface((s) => s.surface)
  const rtPath = useSurface((s) => s.rtPath)
  const internal = useIdentity((s) => s.tier === 'internal')
  if (internal) {
    return (
      <Badge variant="outline" className="h-6 gap-1" data-role-chip="internal" data-identity-badge="">
        <Icon icon="identity.shield" />
        {compact ? null : t('identity.badge')}
      </Badge>
    )
  }
  // D2.2-W2: a city's recorded show (surface 'shared', a `fake:` replay rtPath) is labelled as a simulated replay
  const replay = isCityReplay(surface, rtPath)
  const key = surface === 'sandbox' ? 'role.sandbox' : surface === 'static' ? 'role.static' : surface === 'local' ? 'role.local'
    : replay ? 'role.cityReplay' : 'role.shared'
  const icon: IconKey = surface === 'sandbox' ? 'sandbox.mine' : surface === 'static' ? 'layer.pointcloud' : replay ? 'tl.history' : 'layer.visible'
  // D2.2-FX-world: compact (phone top bar) drops the label for every other role, but the live shared demo and a
  // city's recorded replay only differed by this icon's shape there — add a short label for the replay case so it
  // reads from text, not only from recognizing `tl.history` vs `layer.visible`.
  const compactLabel = compact && replay ? t('role.cityReplay.short') : null
  return (
    <Badge variant={surface === 'sandbox' ? 'secondary' : 'outline'} className="h-6 gap-1" data-role-chip={surface}>
      <Icon icon={icon} />
      {compact ? compactLabel : t(key)}
    </Badge>
  )
}

export function MoreMenu() {
  const t = useT()
  const surface = useSurface((s) => s.surface)
  const world = useSurface((s) => s.worldId)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="icon" variant="ghost" aria-label={t('mobile.more')} data-mobile-more="" />}>
        <Icon icon="ui.more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t('mobile.more')}</DropdownMenuLabel>
          {surface !== 'sandbox' ? (
            <DropdownMenuItem onClick={() => sandboxDialogs.open('start', { world: surface === 'static' ? world : null })} data-mobile-open-sandbox="">
              <Icon icon="sandbox.mine" />{t('landing.sandbox.open')}
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuItem onClick={() => navigate('/worlds')}><Icon icon="nav.world" />{t('menu.world.open')}</DropdownMenuItem>
          <DropdownMenuItem onClick={() => updateSearch({ settings: 'general' })} data-mobile-settings=""><Icon icon="nav.settings" />{t('header.settings')}</DropdownMenuItem>
          <DropdownMenuItem onClick={() => navigate('/sandbox/api')}><Icon icon="api.doc" />{t('menu.help.api')}</DropdownMenuItem>
          {surface === 'sandbox' ? <DropdownMenuItem onClick={() => onboarding.start(true)}><Icon icon="help" />{t('menu.help.onboarding')}</DropdownMenuItem> : null}
        </DropdownMenuGroup>
        {surface === 'sandbox' ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuGroup data-mobile-session="">
              <DropdownMenuLabel>{t('role.sandbox')}</DropdownMenuLabel>
              <DropdownMenuItem onClick={() => void sandbox.keepalive()}><Icon icon="tl.clock" />{t('sandbox.bar.renew')}</DropdownMenuItem>
              <DropdownMenuItem onClick={() => sandboxDialogs.open('template')}>{t('sandbox.bar.template')}</DropdownMenuItem>
              <DropdownMenuItem onClick={() => sandboxDialogs.open('world')}>{t('sandbox.bar.world')}</DropdownMenuItem>
              <DropdownMenuItem onClick={() => sandboxDialogs.open('pair')} data-mobile-pair=""><Icon icon="device.pair" />{t('pair.title')}</DropdownMenuItem>
              <DropdownMenuItem onClick={() => confirmThen({ id: 'sandbox.end', title: t('sandbox.end.title'), body: t('sandbox.end.body'), action: t('sandbox.bar.end'),
                run: () => void sandbox.end() })}><Icon icon="logout" />{t('sandbox.bar.end')}</DropdownMenuItem>
            </DropdownMenuGroup>
          </>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem onClick={() => overlays.set('about', true)}><Icon icon="help" />{t('menu.help.about')}</DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// ------------------------------------------------------------ long press (AWR-04 §15.8.4 row 5)
interface Pressed { clientX: number; clientY: number; point: [number, number, number] | null; drone: string | null }

/** the vehicle's ENU position from the swarm columns */
function posOf(id: string): [number, number, number] | null {
  const rt = rtClient()
  if (!rt) return null
  const no = rt.roster.agentNoOf(id)
  const sw = rt.swarm
  for (let i = 0; i < sw.n; i++) if (sw.agentNo[i] === no) return [sw.pos[3 * i], sw.pos[3 * i + 1], sw.pos[3 * i + 2]]
  return null
}

export function LongPressMenu() {
  const t = useT()
  const write = useD2Write()
  const sandbox = useSurface((s) => s.surface === 'sandbox')
  const [at, setAt] = React.useState<Pressed | null>(null)
  React.useEffect(() => touch.onLongPress((p) => {
    void p.pick.then((r) => {
      const point = r.kind === 'ground' || r.kind === 'point' || r.kind === 'drone' ? [r.pointEnu[0], r.pointEnu[1], r.pointEnu[2]] as [number, number, number] : null
      setAt({ clientX: p.clientX, clientY: p.clientY, point, drone: r.kind === 'drone' ? r.id : null })
    })
  }), [])
  const primary = selectionStore.getState().primary
  const flyHere = (): void => {
    const rt = rtClient()
    if (!at?.point || !primary || !rt) return
    const z = gotoAltitude(at.point[2], posOf(primary)?.[2] ?? null)
    rt.call(`uav/${primary}/cmd/goto`, { pos: [at.point[0], at.point[1], z], route: 'auto' })
    notify('longpress:goto', 'info', t('longpress.sent', { id: primary }))
  }
  const measure = (): void => {
    if (!at?.point) return
    const from = primary ? posOf(primary) : null
    const d = from ? Math.hypot(at.point[0] - from[0], at.point[1] - from[1], at.point[2] - from[2]) : Number.NaN
    notify('longpress:measure', 'info', from ? t('longpress.distance', { id: primary ?? '', m: fmt.num(d, 1) }) : t('longpress.noVehicle'))
  }
  const place = (): void => {
    if (!at?.point) return
    void targetsActions.add('person', [at.point[0], at.point[1], null]).then((o) => {
      if (o.ok) entities.show('targets')
    })
  }
  const copy = (): void => {
    if (!at?.point) return
    void copyText(fmt.enu(at.point[0], at.point[1], at.point[2])).then((ok) => notify('longpress:copy', ok ? 'success' : 'warning', t(ok ? 'longpress.copied' : 'build.copyFailed')))
  }
  return (
    <DropdownMenu open={at !== null} onOpenChange={(o) => (o ? null : setAt(null))}>
      <DropdownMenuTrigger render={<span aria-hidden="true" className="pointer-events-none fixed size-px" style={{ left: at?.clientX ?? 0, top: at?.clientY ?? 0 }} data-longpress-anchor="" />} />
      <DropdownMenuContent className="w-56" data-longpress-menu="">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="font-mono">{at?.point ? fmt.enu(at.point[0], at.point[1], at.point[2]) : t('longpress.noPoint')}</DropdownMenuLabel>
          {write ? <DropdownMenuItem disabled={!at?.point || !primary} onClick={flyHere} data-longpress="goto"><Icon icon="cmd.goto" />{t('longpress.goto')}</DropdownMenuItem> : null}
          {write && sandbox ? <DropdownMenuItem disabled={!at?.point} onClick={place} data-longpress="place"><Icon icon="target.person" />{t('longpress.place')}</DropdownMenuItem> : null}
          <DropdownMenuItem disabled={!at?.point} onClick={measure} data-longpress="measure"><Icon icon="tool.measure" />{t('longpress.measure')}</DropdownMenuItem>
          <DropdownMenuItem disabled={!at?.point} onClick={copy} data-longpress="copy"><Icon icon="copy" />{t('longpress.copy')}</DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
