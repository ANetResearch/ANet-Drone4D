// Fleet list, the DroneRail page 1 (M15-FR-024; AWR-14 §4.4, §6.2, §11.3; D1-AC-27): search (id prefix then substring),
// filter menu (state group, owner, alerts), sort, and a virtualised list (@tanstack/react-virtual, 56 px rows, overscan
// 10: rendered rows <= visible + 10) over the typed rows of stores/fleet (M11), refreshed when the rail key (row count
// and row signatures) changes, checked at the fleet summary rate (Tier S 4 Hz; ADR-072). A row shows the id, the
// shape-coded FlightState badge (warning outline, critical outline, or solid red for the one owner of the rail's one
// red), the owner icon, the altitude and speed as C-class bound text (swarm columns of the page RtClient, rows bind only
// while mounted) and the battery as a D-class MotionNumber with its bucketed icon. List
// selection is bg-muted plus a foreground bar (never red; the focused row's bar is thicker). Click selects and opens
// the detail page, Mod+click toggles, Shift+click selects the range; double click focuses the camera. With two or more
// selected the batch bar sends one fleet/cmd call per action. Row roots carry data-rail-row for DOM counting.
import * as React from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { LAYOUT } from '@/lib/tokens/input.gen'
import { cn } from '@/lib/utils'
import { rtClient } from '@/net/rt'
import { camera } from '@/viewport/facade'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/ui/components/ui/input-group'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import type { IconKey } from '@/ui/icons/registry'
import { useBucketedIcon } from '@/ui/icons/bucket'
import { BoundText } from '@/ui/motion/BoundText'
import { MotionNumber } from '@/ui/motion/MotionNumber'
import { PanelEmpty } from '@/ui/brand'
import { UX } from '@/ui/testing/uxProbe'
import { FlightStateBadge } from '@/ui/notify/StatusBadge'
import { useRedOwner } from '@/ui/notify/redFigures'
import { connViewStore, isOnline, useConnView } from '@/ui/shell/connView'
import { otherWorld } from '@/ui/shell/guards'
import { useRoute, navigate } from '@/app/router/router'
import { fleetIdOf, fleetRows, useFleet } from '@/stores/fleet'
import { prefs } from '@/stores/prefs'
import { selection, selectionStore, useSelection } from '@/stores/selection'
import { BatchBar } from './BatchBar'
import { buildOrder, railStore, useRail, type RailSort, type StateGroup } from './railModel'

/** owner enum -> icon (AWR-14 §6.12) */
export const OWNER_ICON: readonly (IconKey | null)[] = [null, 'user', 'nav.mission', 'agent', 'cmd.formation', 'alert.geofence', 'mode.manual', 'external']

/** swarm column index of an agent, starting from the row index (the swarm order is usually the row order) */
function swarmIndex(agentNo: number, hint: number): number {
  const rt = rtClient()
  if (!rt) return -1
  const sw = rt.swarm
  if (hint < sw.n && sw.agentNo[hint] === agentNo) return hint
  for (let i = 0; i < sw.n; i++) if (sw.agentNo[i] === agentNo) return i
  return -1
}
function altOf(agentNo: number, hint: number): number {
  const i = swarmIndex(agentNo, hint)
  return i < 0 ? Number.NaN : rtClient()!.swarm.pos[3 * i + 2]
}
function speedOf(agentNo: number, hint: number): number {
  const i = swarmIndex(agentNo, hint)
  if (i < 0) return Number.NaN
  const vel = rtClient()!.swarm.vel
  return Math.hypot(vel[3 * i], vel[3 * i + 1], vel[3 * i + 2])
}
const fmtAlt = (v: number) => (Number.isFinite(v) ? `${fmt.num(v, 1)} m` : fmt.num(v))
/** data age in seconds while the link is down (the rows keep their last values with STALE, AWR-14 §7.9) */
const staleOf = () => {
  const since = connViewStore.getState().downSinceMs
  return Number.isFinite(since) ? Math.max(0.1, (Date.now() - since) / 1000) : 0
}

/**
 * what a row shows from the typed columns, as one number: a row re-renders only when its own state, battery, owner or
 * flags change, not on every fleet sample (P4-UI, D1-AC-23; the altitude and speed are bound text and need no render)
 */
export function rowSignature(i: number): number {
  const r = fleetRows
  return (((((r.fs[i] & 31) * 8 + (r.sub[i] & 7)) * 256 + r.flags[i]) * 256 + r.battery[i]) * 8 + (r.owner[i] & 7)) * 4 + (r.alert[i] & 1) * 2 + (r.stale[i] & 1)
}

/** sample time of the current fleet rows on the performance.now() clock (the battery icon dwell, ui/icons/bucket.ts) */
const rowSample = { ms: 0 }

interface RowProps { i: number; id: string; sig: number; selected: boolean; focused: boolean; red: boolean; onRow: (id: string, e: React.MouseEvent) => void }

const Row = React.memo(function Row({ i, id, selected, focused, red, onRow }: RowProps) {
  const t = useT()
  const sampleMs = rowSample.ms
  const agentNo = fleetRows.agentNo[i]
  const fs = fleetRows.fs[i]
  const bat = fleetRows.battery[i]
  const owner = fleetRows.owner[i]
  const alert = fleetRows.alert[i] === 1
  const flags = fleetRows.flags[i]
  const stale = fleetRows.stale[i] === 1
  const readAlt = React.useCallback(() => altOf(agentNo, i), [agentNo, i])
  const readSpeed = React.useCallback(() => speedOf(agentNo, i), [agentNo, i])
  const batIcon = useBucketedIcon('battery', bat === 255 ? Number.NaN : bat, sampleMs, alert)
  const ownerIcon = OWNER_ICON[owner] ?? null
  return (
    <div data-rail-row="" data-drone-id={id} data-selected={selected ? '' : undefined} data-focused={focused ? '' : undefined} data-alert={alert ? '' : undefined} data-stale={stale ? '' : undefined}
      tabIndex={-1} onClick={(e) => onRow(id, e)} onDoubleClick={() => camera.focus([id])}
      className={cn('lf-sel-bar flex h-full cursor-pointer flex-col justify-center gap-1 rounded-md px-2 hover:bg-muted/50', selected && 'bg-muted', focused && 'shadow-[inset_3px_0_0_var(--foreground)]')}>
      <div className="flex min-w-0 items-center gap-1.5">
        <Icon icon="drone.quad" className="shrink-0" />
        <span className="truncate font-mono text-xs">{id}</span>
        <FlightStateBadge fs={fs} sub={fleetRows.sub[i]} flags={flags} redOwner={red} short className="ml-auto h-4 shrink-0 px-1.5 text-hud-cap" />
        {ownerIcon ? <Icon icon={ownerIcon} label={t(`owner.${owner}`)} className="shrink-0 text-muted-foreground" /> : null}
      </div>
      {/* the telemetry line is one raster island per row (ADR-066): its 4 Hz text writes never re-raster the rail */}
      <div className="flex items-center gap-2 text-hud-sub text-muted-foreground" data-island="">
        <BoundText read={readAlt} format={fmtAlt} stale={staleOf} className="w-16" island={false} />
        <BoundText read={readSpeed} format={fmt.speed} className="w-16" island={false} />
        <span className="ml-auto inline-flex items-center gap-1">
          <StateIcon icon={batIcon} spring="hud" label={t('drones.batteryLabel')} />
          {bat === 255 ? <span>{t('drones.batteryNone')}</span> : <MotionNumber value={bat} format={(v) => fmt.pct(v)} island={false} />}
        </span>
      </div>
    </div>
  )
})

const GROUPS: readonly StateGroup[] = ['all', 'air', 'ground', 'alert']
const SORTS: readonly RailSort[] = ['id', 'severity', 'battery']
const OWNERS = [-1, 0, 1, 2, 3, 4, 5]

/**
 * the panel shell: search, filter menu and the empty and connecting states. It does not subscribe to the fleet samples;
 * only FleetList below does, so the 4 Hz fleet summary re-renders the list rows that changed and never the search field
 * and the filter menu (P4-UI, D1-AC-23: their Base UI hidden inputs rewrote attributes on every render)
 */
export function DronesPanel() {
  const t = useT()
  const n = useFleet((s) => s.n)
  // the selection reaches the rows and the batch bar (four CallButtons with tooltip, menu and confirm machinery) in a
  // deferred render: a selection key press (select all on 1000 rows) re-renders only this component's shell with the
  // previous value inside the key handler, and React highlights the rows and mounts the bar in its time-sliced pass
  // (ADR-069; D1-AC-27)
  const ids = React.useDeferredValue(useSelection((s) => s.ids))
  const showBatch = ids.length >= 2
  const filter = useRail((s) => s)
  const conn = useConnView((s) => s.conn)
  const connecting = n === 0 && (conn === 'CONNECTING' || conn === 'SYNCING')
  useRoute() // the view world of the route
  const session = useConnView((s) => s.sessionWorldId)
  if (otherWorld() !== null && session) {
    // static browsing of another world: no simulation runs here (AWR-14 §6.16, §7.2)
    return <PanelEmpty title={t('drones.otherWorld')} description={t('world.mismatch', { world: session })}
      action={<Button size="sm" variant="outline" onClick={() => navigate(`/world/${session}`)}>{t('world.backTo', { world: session })}</Button>} />
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2" data-drone-list="">
      <div className="flex items-center gap-1">
        <InputGroup className="h-7">
          <InputGroupAddon><Icon icon="cmd.search" /></InputGroupAddon>
          <InputGroupInput value={filter.query} onChange={(e) => railStore.setState({ query: e.target.value })} placeholder={t('drones.search')} aria-label={t('drones.search')} />
        </InputGroup>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button size="icon-sm" variant={filter.group !== 'all' || filter.owner >= 0 ? 'secondary' : 'outline'} aria-label={t('drones.filter')} />}>
            <Icon icon="filter" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuGroup>
              <DropdownMenuLabel>{t('drones.filter.state')}</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={filter.group} onValueChange={(g) => railStore.setState({ group: g as StateGroup })}>
                {GROUPS.map((g) => <DropdownMenuRadioItem key={g} value={g}>{t(`drones.group.${g}`)}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel>{t('drones.filter.owner')}</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={String(filter.owner)} onValueChange={(o) => railStore.setState({ owner: Number(o) })}>
                {OWNERS.map((o) => <DropdownMenuRadioItem key={o} value={String(o)}>{o < 0 ? t('drones.owner.all') : t(`owner.${o}`)}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel>{t('drones.sort')}</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={filter.sort} onValueChange={(s) => railStore.setState({ sort: s as RailSort })}>
                {SORTS.map((s) => <DropdownMenuRadioItem key={s} value={s}>{t(`drones.sort.${s}`)}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {connecting ? (
        <div className="flex flex-col gap-2" data-rail-skeleton="">{[0, 1, 2].map((k) => <Skeleton key={k} className="h-12 rounded-md" />)}</div>
      ) : n === 0 ? (
        <PanelEmpty title={t('drones.empty')} description={t(isOnline(conn) ? 'drones.emptyLive' : 'drones.emptyHint')} />
      ) : <FleetList ids={ids} />}
      {showBatch ? <BatchBar /> : null}
    </div>
  )
}

function onRow(id: string, e: React.MouseEvent): void {
  if (e.shiftKey) {
    const cur = selectionStore.getState().primary
    const list = Array.from(buildOrder(fleetRows, railStore.getState(), fleetIdOf), (x) => fleetIdOf(x))
    if (cur) selection.selectRange(cur, id, list)
    else selection.select([id])
  } else if (e.ctrlKey || e.metaKey) selection.select([id], 'toggle')
  else {
    selection.select([id])
    prefs.setLayout({ right: { page: 'detail' } })
  }
}

/**
 * a hash of what the rail shows (row count and every row signature): FleetList re-renders when it changes, not on every
 * fleet sample (4 Hz); altitude and speed are bound text and change without a render (P4-UI, D1-AC-23)
 */
export function railKey(s: { n: number }): number {
  let h = (s.n * 2654435761) | 0
  for (let i = 0; i < s.n; i++) h = (Math.imul(h ^ rowSignature(i), 16777619) + i) | 0
  return h
}

/** the count line and the virtualised rows; the only part of the rail that follows the fleet samples */
function FleetList({ ids }: { ids: readonly string[] }) {
  const t = useT()
  const version = useFleet(railKey)
  const n = useFleet((s) => s.n)
  const primary = React.useDeferredValue(useSelection((s) => s.primary))
  const filter = useRail((s) => s)
  const redOwner = useRedOwner('drone-rail')
  const viewportRef = React.useRef<HTMLDivElement>(null)
  // fleetRows are columnar and updated in place: the rail key is the cache key of the order and of the sample time
  // oxlint-disable-next-line react-hooks/exhaustive-deps -- see above
  const order = React.useMemo(() => buildOrder(fleetRows, filter, fleetIdOf), [n, filter, version])
  React.useMemo(() => {
    rowSample.ms = performance.now()
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- one sample time per change of the rail key
  }, [version])
  const selected = React.useMemo(() => new Set(ids), [ids])
  // oxlint-disable-next-line react/incompatible-library -- ADR-028: TanStack Virtual; the compiler skips this component
  const v = useVirtualizer({ count: order.length, getScrollElement: () => viewportRef.current, estimateSize: () => LAYOUT.railRowPx, overscan: LAYOUT.railOverscan })
  const items = v.getVirtualItems()
  const rendered = items.length
  React.useEffect(() => {
    UX.droneRail.renderedRows = rendered
    UX.droneRail.visibleRows = Math.ceil((viewportRef.current?.clientHeight ?? 0) / LAYOUT.railRowPx)
  }, [rendered])
  // keep the focused row visible after a selection from elsewhere (3D, events, palette; AWR-14 §6.2 rule 3)
  React.useEffect(() => {
    if (!primary) return
    let k = -1
    for (let x = 0; x < order.length; x++) if (fleetIdOf(order[x]) === primary) k = x
    if (k >= 0) v.scrollToIndex(k, { align: 'auto' })
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- only when the focus changes
  }, [primary])
  return (
    <>
      <div className="flex items-center justify-between text-hud-cap uppercase text-muted-foreground">
        <span>{t('drones.count', { shown: fmt.count(order.length), n: fmt.count(n) })}</span>
        {ids.length ? <Badge variant="outline">{t('drones.selected', { n: ids.length })}</Badge> : null}
      </div>
      {/* edge fades as overlays, not a mask (ADR-066): the rows are raster islands, and a mask over composited rows costs
          an offscreen render pass every frame (styles/layout.css fade-scroll-y) */}
      <div className="fade-scroll-y flex min-h-0 flex-1 flex-col">
        <div ref={viewportRef} className="min-h-0 flex-1 overflow-auto" data-fade-scroller="" data-figure="drone-rail" role="list" aria-label={t('panel.drones.title')}>
          <div style={{ height: v.getTotalSize(), position: 'relative' }}>
            {items.map((it) => {
              const i = order[it.index]
              const id = fleetIdOf(i)
              return (
                <div key={it.key} role="listitem" className="absolute inset-x-0" style={{ transform: `translateY(${it.start}px)`, height: LAYOUT.railRowPx }}>
                  <Row i={i} id={id} sig={rowSignature(i)} selected={selected.has(id)} focused={primary === id} red={redOwner?.id === id} onRow={onRow} />
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </>
  )
}
