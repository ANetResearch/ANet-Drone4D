// Targets list, the right rail's second entity (R-D2-07, R-D2-18; M15-FR-212; UX-FR-119, UX-FR-120, UX-FR-123;
// AWR-14 §20.7.1, §20.7.2, §20.7.5): "targets n / 30" with "add" (class menu; places the target with a viewport click on
// the ground, TARGET_PLACE) for the sandbox operator and a filter menu (class, awareness, best level, discovered only).
// Rows: class icon, label (sanitised), motion mode, best capture level and "captured", awareness, discovered count (an
// outlined badge; the latest unconfirmed discovery is the list's one red). A row click selects the target (never the
// vehicle selection) and opens its detail page. The shared show lists the targets of the run read-only; static browsing
// shows the empty state. Data: M18's stores/targets.ts (TargetLite32 rows: awareness, best level, capture, discovered
// count; the configurations of the details), the actions target/add, update, remove (WS calls of the sandbox seat or the
// full-function identity); in a sandbox the REST list seeds the configurations once so the motion modes and the detail
// form are known before the first detail message.
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { sanitizeText } from '@/lib/sanitize'
import { cn } from '@/lib/utils'
import { targetsActions, useTargets, type TargetCallOutcome } from '@/stores/targets'
import { useFleet } from '@/stores/fleet'
import { useRt } from '@/ui/shell/RtContext'
import { targetsModel } from '@/app/shell/engineD2'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/ui/components/ui/dropdown-menu'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { PanelEmpty } from '@/ui/brand'
import { useSurface } from '@/app/shell/surface'
import { useRedOwner } from '@/ui/notify/redFigures'
import { sbSession } from '@/ui/sandbox/domain'
import { entities, useEntities } from '@/ui/sandbox/entities'
import { useSandbox } from '@/ui/sandbox/session'
import { d2Tools, useD2Tool } from '@/ui/tools/d2Tools'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { buildRows, filterRows, itemsFromDetails, NO_FILTER, RosterTargetGate, TARGET_CLASSES, type TargetClass, type TargetFilter, type TargetRow } from './targetModel'

export const CLASS_ICON: Readonly<Record<TargetClass, IconKey>> = {
  person: 'target.person', dog: 'target.dog', vehicle: 'target.vehicle', uav: 'target.uav', machine: 'target.machine', object: 'target.object',
}

/** the sandbox's REST target list seeds the configurations of the target model once per session (operator) */
export function useSeedTargetConfigs(enabled: boolean): void {
  const sid = useSandbox((s) => s.sid)
  React.useEffect(() => {
    if (!enabled || !sid) return
    let live = true
    void sbSession.targets().then((r) => {
      if (!live) return
      for (const c of r.items ?? []) if (c.id && !targetsModel.cfgs.has(c.id)) targetsModel.setConfig(c as Parameters<typeof targetsModel.setConfig>[0])
    }, () => {
      // the detail messages bring the configurations anyway (cfg_rev, every 10 s)
    })
    return () => {
      live = false
    }
  }, [enabled, sid])
}

/** the targets of the page: the store's TargetLite32 rows, else the roster entries and seeded configurations with the
 *  state of the detail messages (targetModel.itemsFromDetails) */
export function useTargetList() {
  const rt = useRt()
  const storeItems = useTargets((s) => s.items)
  const version = useTargets((s) => s.version)
  const rosterVersion = useFleet((s) => s.rosterVersion)
  const sid = useSandbox((s) => s.sid)
  return React.useMemo(() => {
    if (storeItems.length) return storeItems
    const entries = rt.roster.targets?.() ?? []
    return itemsFromDetails(entries, targetsModel.details, targetsModel.cfgs, rosterGate.forSession(sid).update(entries))
  },
  // the target model and the roster are read with their versions
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  [storeItems, rt, rosterVersion, version, sid])
}

/** one roster gate per page, reset with the sandbox session (ids recur across sessions and worlds) */
const rosterGate = {
  gate: new RosterTargetGate(),
  sid: null as string | null,
  forSession(sid: string | null): RosterTargetGate {
    if (sid !== this.sid) {
      this.sid = sid
      this.gate.reset()
    }
    return this.gate
  },
}

/** reason text of a refused target call, with the `why` of 540 to 544 when given */
export function targetCallText(o: TargetCallOutcome): string {
  const why = typeof o.detail?.why === 'string' ? o.detail.why : null
  return `${o.code ? reasonText(o.code).short : ''}${why ? ` (${sanitizeText(why, 32)})` : ''}`
}

/** place a target of a class with a viewport click (TARGET_PLACE); 540 keeps the tool with the reason */
export function startPlaceTarget(cls: TargetClass, onCreated: (id: string) => void): void {
  targetsActions.setPlacing(cls)
  d2Tools.start('TARGET_PLACE', cls, ([p]) => {
    targetsActions.setPlacing(null)
    void targetsActions.add(cls, [p[0], p[1], cls === 'uav' ? p[2] + 30 : null]).then((o) => {
      if (o.ok) {
        if (o.id) onCreated(o.id)
        return
      }
      notify('targets:place', 'warning', targetCallText(o))
      if (o.code === 540) startPlaceTarget(cls, onCreated)
    })
  })
}

function Row({ r, selected, red, onClick }: { r: TargetRow; selected: boolean; red: boolean; onClick: () => void }) {
  const t = useT()
  return (
    <div data-target-row={r.id} data-selected={selected ? '' : undefined} tabIndex={0} onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onClick()
      }}
      className={cn('lf-sel-bar flex cursor-pointer flex-col gap-1 rounded-md px-2 py-1.5 hover:bg-muted/50', selected && 'bg-muted')}>
      <div className="flex min-w-0 items-center gap-1.5">
        <Icon icon={CLASS_ICON[r.cls]} className="shrink-0" label={t(`target.class.${r.cls}`)} />
        <span className="truncate text-xs font-medium">{sanitizeText(r.label, 32)}</span>
        <span className="text-hud-cap text-muted-foreground">{t(`target.motion.${r.motion}`)}</span>
        {r.discovered > 0 ? (
          <Badge variant={red ? 'destructive' : 'outline'} className={cn('ml-auto h-4 gap-0.5 px-1.5 text-hud-cap', !red && 'border-brand text-brand-text')} data-target-discovered={r.discovered}>
            <Icon icon="alert.warning" />
            {r.discovered}
          </Badge>
        ) : null}
      </div>
      <div className="flex items-center gap-2 text-hud-sub text-muted-foreground">
        <span data-target-level={r.best}>{t(`perception.best.${r.best}`)}</span>
        {r.captured ? <Badge variant="secondary" className="h-4 gap-0.5 px-1.5 text-hud-cap" data-target-captured=""><Icon icon="effect.verified" />{t('target.captured')}</Badge> : null}
        <span className="ml-auto" data-target-awareness={r.awareness}>{t(`awareness.${r.awareness}`)}</span>
      </div>
    </div>
  )
}

export function TargetsPanel() {
  const t = useT()
  const surface = useSurface((s) => s.surface)
  // writes follow the session role (sandbox operator, or the D2.1 full-function identity on the shared show)
  const operator = useD2Write()
  const items = useTargetList()
  const limit = useTargets((s) => s.max)
  const version = useTargets((s) => s.version)
  useSeedTargetConfigs(surface === 'sandbox')
  const selected = useEntities((s) => s.target)
  const placing = useD2Tool((s) => (s.mode === 'TARGET_PLACE' ? s.subject : null))
  const red = useRedOwner('viewport')
  const [filter, setFilter] = React.useState<TargetFilter>(NO_FILTER)
  // the configurations of the model are read with the store version (motion modes of the rows)
  const rows = React.useMemo(() => filterRows(buildRows(items, (id) => targetsModel.cfgs.get(id)?.motion?.mode), filter),
    // oxlint-disable-next-line react-hooks/exhaustive-deps
    [items, filter, version])
  if (surface === 'static' || surface === 'sandbox-gone') return <PanelEmpty brand={false} title={t('targets.static')} description={t('hint.staticBrowse')} />
  const full = items.length >= limit
  return (
    <div className="flex min-h-0 flex-col gap-2" data-targets-panel="">
      <div className="flex items-center gap-1.5">
        <span className="text-hud-cap font-semibold uppercase text-muted-foreground" data-targets-count={items.length}>{t('targets.count', { n: items.length, max: limit })}</span>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button size="xs" variant="ghost" className="ml-auto" aria-label={t('targets.filter')} data-targets-filter="" />}>
            <Icon icon="filter" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuGroup>
              <DropdownMenuLabel>{t('targets.filter.class')}</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={filter.cls} onValueChange={(v) => setFilter({ ...filter, cls: v as TargetFilter['cls'] })}>
                <DropdownMenuRadioItem value="all">{t('common.all')}</DropdownMenuRadioItem>
                {TARGET_CLASSES.map((c) => <DropdownMenuRadioItem key={c} value={c}>{t(`target.class.${c}`)}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel>{t('targets.filter.level')}</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={filter.best} onValueChange={(v) => setFilter({ ...filter, best: v as TargetFilter['best'] })}>
                <DropdownMenuRadioItem value="all">{t('common.all')}</DropdownMenuRadioItem>
                {(['none', 'D', 'R', 'I'] as const).map((l) => <DropdownMenuRadioItem key={l} value={l}>{t(`perception.best.${l}`)}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel>{t('targets.filter.awareness')}</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={filter.awareness} onValueChange={(v) => setFilter({ ...filter, awareness: v as TargetFilter['awareness'] })}>
                <DropdownMenuRadioItem value="all">{t('common.all')}</DropdownMenuRadioItem>
                {(['UNAWARE', 'SUSPICIOUS', 'REACTING', 'ALERTED', 'CALMING'] as const).map((a) => <DropdownMenuRadioItem key={a} value={a}>{t(`awareness.${a}`)}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
              <DropdownMenuCheckboxItem checked={filter.discoveredOnly} onCheckedChange={(c) => setFilter({ ...filter, discoveredOnly: c === true })}>{t('targets.filter.discovered')}</DropdownMenuCheckboxItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
        {operator ? (
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button size="xs" variant="outline" disabled={full} data-targets-add="" />}>
              <Icon icon="plus" data-icon="inline-start" />
              {t('targets.add')}
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                <DropdownMenuLabel>{t('targets.add.class')}</DropdownMenuLabel>
                {TARGET_CLASSES.map((c) => (
                  <DropdownMenuItem key={c} onClick={() => startPlaceTarget(c, (id) => entities.openTarget(id))} data-targets-add-class={c}>
                    <Icon icon={CLASS_ICON[c]} />
                    {t(`target.class.${c}`)}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </div>
      {full && operator ? <p className="text-hud-sub text-muted-foreground" data-targets-full="">{reasonText(505).short}</p> : null}
      {placing ? (
        <p className="flex items-center gap-1.5 text-hud-sub" data-targets-placing={placing}>
          <Icon icon="mission.target" />
          {t('targets.placing', { cls: t(`target.class.${placing}`) })}
          <Button size="xs" variant="ghost" className="ml-auto" onClick={() => {
            d2Tools.end()
            targetsActions.setPlacing(null)
          }}>{t('common.cancel')}</Button>
        </p>
      ) : null}
      {rows.length === 0 ? (
        <PanelEmpty brand={false} title={t('targets.empty')} description={operator ? t('targets.emptyHint') : t('targets.emptyViewer')} />
      ) : (
        <div className="flex min-h-0 flex-col gap-0.5 overflow-y-auto" data-targets-list={rows.length}>
          {rows.map((r) => <Row key={r.id} r={r} selected={r.id === selected} red={red?.kind === 'target' && red.id === r.id} onClick={() => entities.openTarget(r.id)} />)}
        </div>
      )}
    </div>
  )
}
