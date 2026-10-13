// Nests, the right rail's third entity (R-D2-16, R-D2-26; AWR-04 §8.2; M15-FR-213; UX-FR-125; AWR-14 §20.8): the list
// (kind icon, name, slots, inventory, energy mode; "needs ground crew, not part of unattended 7x24" for a crewed
// launcher) with "add" (kind menu, then a viewport click on the ground or a roof, NEST_PLACE; at most 4, code 505) and
// the detail page: the configuration (name, slots, energy mode and swap time, chargers with cells_max checked against the
// battery packs, packs, turnaround, launch interval, wind limit, RTK base station, link range), the live status of
// `nest/{id}/status` (1 Hz: slots, inventory, one 20-tick gauge per charger channel with the time left, the battery queue,
// the launch queue; "dispatch delayed (battery)" outlined when no pack is ready) and removal (code 105 while vehicles
// are inside). Data: M20's stores/tasking.ts (nests of the sandbox REST, `nest/*/status` at 1 Hz, the add, update and remove
// actions); the shared show lists the nests of its status topics read-only (S7), the write controls follow `canEdit`
// (operator or admin, the D2.1 full-function identity included; disabled while the session is stale).
import type { Nest, NestStatus } from '@awr/contracts/types'
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { cn } from '@/lib/utils'
import { nestPacks, taskingActions, useTasking, type TaskingOutcome } from '@/stores/tasking'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Switch } from '@/ui/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { LfTickGauge } from '@/ui/lf/LfTickGauge'
import { PanelEmpty } from '@/ui/brand'
import { confirmThen } from '@/ui/actions/ConfirmHost'
import { useSurface } from '@/app/shell/surface'
import { NumField, TextField } from '@/ui/sandbox/fields'
import { entities, useEntities } from '@/ui/sandbox/entities'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { d2Tools, useD2Tool } from '@/ui/tools/d2Tools'
import { labelOk } from '@/ui/panels/targets/targetModel'
import { channelRows } from './nestModel'

export type NestKind = NonNullable<Nest['kind']>
export const NEST_KINDS: readonly NestKind[] = ['box', 'launcher', 'vtol_pad']
export const NEST_ICON: Readonly<Record<NestKind, IconKey>> = { box: 'nest.box', launcher: 'nest.launcher', vtol_pad: 'nest.vtol' }
export const MAX_NESTS = 4

/** the creation body of a nest kind at a ground point (AWR-04 §8.2 defaults; the server fills the rest) */
export function nestDefaults(kind: NestKind, x: number, y: number): Nest {
  const base: Nest = { pos: [x, y, null], kind, crewed: kind === 'launcher', slots: kind === 'launcher' ? 2 : 4 }
  if (kind === 'launcher') return { ...base, energy: { mode: 'swap', swap_s: 300 } }
  return { ...base, energy: { mode: 'swap', swap_s: 180, packs: [{ cells: 6, count: 6, capacity_ah: 10, chemistry: 'lihv' }] },
    chargers: [{ model: 'c1_xr', count: 4, channels: 1, i_max_a: 10, cells_max: 6 }] }
}

/** chargers that cannot charge the packs of the nest (cells_max below the pack cells; the server answers 525) */
export function chargerMismatch(n: Pick<Nest, 'chargers' | 'energy'>): boolean {
  const cells = Math.max(0, ...(n.energy?.packs ?? []).map((p) => p.cells))
  return (n.chargers ?? []).some((c) => c.cells_max < cells)
}

/** the nests of the store: the sandbox's configurations, or (shared show) one stub per status topic */
export function nestList(nests: readonly Nest[], status: Readonly<Record<string, NestStatus>>): Nest[] {
  if (nests.length) return [...nests]
  return Object.values(status).map((st) => ({ id: st.id, kind: st.kind, pos: [null, null, null] as Nest['pos'], slots: st.slots.total, inventory: st.inventory }))
}

export function useNests(): Nest[] {
  const nests = useTasking((s) => s.nests)
  const status = useTasking((s) => s.nestStatus)
  return React.useMemo(() => nestList(nests, status), [nests, status])
}

const errText = (o: TaskingOutcome<unknown>): string => (o.code ? reasonText(o.code).short : typeof o.detail?.error === 'string' ? o.detail.error : '')

export function NestsPanel() {
  const t = useT()
  const surface = useSurface((s) => s.surface)
  const canEdit = useD2Write()
  const items = useNests()
  const limit = MAX_NESTS
  const selected = useEntities((s) => s.nest)
  const placing = useD2Tool((s) => (s.mode === 'NEST_PLACE' ? s.subject : null))
  if (surface === 'static' || surface === 'sandbox-gone') return <PanelEmpty brand={false} title={t('nests.viewer')} description={t('hint.staticBrowse')} />
  if (!canEdit && items.length === 0) return <PanelEmpty brand={false} title={t('nests.viewer')} description={t('nests.viewerHint')} />
  const full = items.length >= limit
  const place = (kind: NestKind) => d2Tools.start('NEST_PLACE', kind, ([p]) => void taskingActions.addNest(nestDefaults(kind, p[0], p[1])).then((o) => {
    if (!o.ok) notify('nests:place', 'warning', errText(o))
    else if (o.data?.id) entities.openNest(o.data.id)
  }))
  return (
    <div className="flex min-h-0 flex-col gap-2" data-nests-panel="">
      <div className="flex items-center gap-1.5">
        <span className="text-hud-cap font-semibold uppercase text-muted-foreground" data-nests-count={items.length}>{t('nests.count', { n: items.length, max: limit })}</span>
        {canEdit ? (
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button size="xs" variant="outline" className="ml-auto" disabled={full} data-nests-add="" />}>
              <Icon icon="plus" data-icon="inline-start" />{t('nests.add')}
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                <DropdownMenuLabel>{t('nests.add.kind')}</DropdownMenuLabel>
                {NEST_KINDS.map((k) => <DropdownMenuItem key={k} onClick={() => place(k)} data-nests-add-kind={k}><Icon icon={NEST_ICON[k]} />{t(`nest.kind.${k}`)}</DropdownMenuItem>)}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </div>
      {full && canEdit ? <p className="text-hud-sub text-muted-foreground">{reasonText(505).short}</p> : null}
      {placing ? (
        <p className="flex items-center gap-1.5 text-hud-sub" data-nests-placing={placing}>
          <Icon icon="mission.target" />{t('nests.placing', { kind: t(`nest.kind.${placing}`) })}
          <Button size="xs" variant="ghost" className="ml-auto" onClick={() => d2Tools.end()}>{t('common.cancel')}</Button>
        </p>
      ) : null}
      {items.length === 0 ? <PanelEmpty brand={false} title={t('nests.empty')} description={t('nests.emptyHint')} /> : (
        <div className="flex min-h-0 flex-col gap-0.5 overflow-y-auto">
          {items.map((n) => {
            const id = n.id ?? ''
            const kind = n.kind ?? 'box'
            return (
              <div key={id} data-nest-row={id} tabIndex={0} onClick={() => entities.openNest(id)} onKeyDown={(e) => e.key === 'Enter' && entities.openNest(id)}
                className={cn('lf-sel-bar flex cursor-pointer flex-col gap-1 rounded-md px-2 py-1.5 hover:bg-muted/50', id === selected && 'bg-muted')}>
                <span className="flex items-center gap-1.5">
                  <Icon icon={NEST_ICON[kind]} label={t(`nest.kind.${kind}`)} />
                  <span className="truncate text-xs font-medium">{sanitizeText(n.name ?? id, 32)}</span>
                  <span className="ml-auto text-hud-cap text-muted-foreground">{t(`nest.energy.${n.energy?.mode ?? 'swap'}`)}</span>
                </span>
                <span className="flex items-center gap-2 text-hud-sub text-muted-foreground">
                  <span>{t('nests.slots', { n: n.slots ?? 0 })}</span>
                  <span className="truncate">{(n.inventory ?? []).map((i) => `${i.model_id} × ${i.count}`).join(t('common.listSep')) || t('nests.noInventory')}</span>
                  {n.crewed ? <Badge variant="outline" className="ml-auto h-4 px-1.5 text-hud-cap">{t('nests.crewed')}</Badge> : null}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function NestStatusView({ id, nest }: { id: string; nest: Nest | null }) {
  const t = useT()
  const st = useTasking((s) => s.nestStatus[id] ?? null)
  // derived from the status object (a selector returning a fresh object would re-render forever)
  const packs = React.useMemo(() => nestPacks({ nestStatus: st ? { [id]: st } : {} } as Parameters<typeof nestPacks>[0], id), [st, id])
  if (!st) return <p className="text-hud-sub text-muted-foreground">{t('common.loading')}</p>
  const ready = packs.ready
  const waiting = (st.launch_queue ?? []).length
  const short = packs.batteryDelay || (ready === 0 && waiting > 0)
  const ch = channelRows(st.chargers)
  const ch0 = nest?.chargers?.[0]
  const cells = nest?.energy?.packs?.[0]?.cells
  return (
    <div className="flex flex-col gap-2 text-hud-sub" data-nest-status={id}>
      <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-muted-foreground">
        <span>{t('nests.slotsUse', { n: st.slots.occupied, of: st.slots.total })}</span>
        {nest?.rtk_base ? <span>{`· ${t('nests.f.rtk')}`}</span> : null}
        {nest?.wind_max_mps !== undefined ? <span>{`· ${t('nests.windMax', { v: fmt.num(nest.wind_max_mps, 0) })}`}</span> : null}
        {!st.wind_ok ? <Badge variant="outline" className="border-dashed">{t('nests.windHold')}</Badge> : null}
      </span>
      {st.inventory.length ? (
        <span className="flex flex-col gap-0.5" data-nest-inventory={st.inventory.length}>
          <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('nests.inventory')}</span>
          {st.inventory.map((i) => (
            <span key={i.model_id} className="font-mono">{t('nests.inventoryRow', { model: sanitizeText(i.model_id, 32), n: i.count, inNest: i.in_nest ?? 0, air: i.airborne ?? 0 })}</span>
          ))}
        </span>
      ) : null}
      {nest?.energy ? (
        <span className="text-muted-foreground" data-nest-energy="">
          {t('nests.energyLine', { mode: t(`nest.energy.${nest.energy.mode}`), swap: fmt.num(nest.energy.swap_s ?? Number.NaN, 0), turn: fmt.num(nest.turnaround_s ?? Number.NaN, 0),
            launch: fmt.num(nest.launch_interval_s ?? Number.NaN, 0) })}
        </span>
      ) : null}
      <div className="flex flex-col gap-1" data-nest-chargers={st.chargers.length}>
        <span className="text-hud-cap font-semibold uppercase text-muted-foreground">
          {ch0 ? t('nests.chargersOf', { model: sanitizeText(ch0.model, 16), n: ch0.count * ch0.channels, cells: cells ?? ch0.cells_max, a: fmt.num(ch0.i_max_a, 0) }) : t('nests.chargers')}
        </span>
        {ch.busy.map((c) => (
          <span key={c.ch} className="flex items-center gap-2" data-nest-charger={c.ch}>
            <span className="w-4 text-right font-mono">{c.ch + 1}</span>
            <LfTickGauge mini value={c.soc_pct ?? 0} ariaLabel={t('nests.charging', { pct: fmt.num(c.soc_pct ?? 0, 0) })} />
            <span className="w-9 text-right font-mono">{`${fmt.num(c.soc_pct ?? 0, 0)}%`}</span>
            <span className="text-muted-foreground">{c.eta_s !== null && c.eta_s !== undefined ? t('nests.eta', { min: fmt.num(c.eta_s / 60, 0) }) : ''}</span>
          </span>
        ))}
        {ch.idle.length ? <span className="text-muted-foreground" data-nest-idle={ch.idle.length}>{t('nests.idleChannels', { list: ch.idle.join(t('common.listSep')) })}</span> : null}
        {ch.blocked.length ? <span data-nest-blocked={ch.blocked.length}>{t('nests.blockedChannels', { list: ch.blocked.join(t('common.listSep')) })}</span> : null}
      </div>
      <span className={cn('rounded-sm px-1', short && 'outline-1 outline-dashed outline-foreground/40')} data-nest-packs={ready}>
        {t('nests.packs', { ready, charging: packs.charging, queued: packs.queued })}
        {cells ? ` (${cells}S)` : ''}
        {short ? ` · ${t('nests.delayBattery')}` : ''}
      </span>
      {(st.aircraft ?? []).length ? (
        <span className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono" data-nest-aircraft={(st.aircraft ?? []).length}>
          {(st.aircraft ?? []).map((a) => (
            <span key={a.uav}>{`${sanitizeText(a.uav, 24)} ${t(`nests.ac.${a.state}`)}${a.soc !== undefined ? ` ${fmt.num(a.soc * 100, 0)}%` : ''}`}</span>
          ))}
        </span>
      ) : null}
      {waiting || (st.recover_queue ?? []).length ? (
        <span className="flex flex-col gap-0.5" data-nest-launch={waiting}>
          <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('nests.launchQueue')}</span>
          {(st.launch_queue ?? []).map((l) => <span key={l.uav} className="font-mono">{`${sanitizeText(l.uav, 32)}${l.tid ? ` → ${sanitizeText(l.tid, 32)}` : ''}${l.delay_reason ? ` · ${t('nests.delay', { why: sanitizeText(l.delay_reason, 32) })}` : ''}`}</span>)}
          {(st.recover_queue ?? []).map((r) => <span key={`r-${r.uav}`} className="font-mono text-muted-foreground">{t('nests.recover', { uav: sanitizeText(r.uav, 32), eta: r.eta_s !== null && r.eta_s !== undefined ? fmt.num(r.eta_s, 0) : '-' })}</span>)}
        </span>
      ) : null}
    </div>
  )
}

export function NestDetailPanel() {
  const t = useT()
  const id = useEntities((s) => s.nest)
  const canEdit = useD2Write()
  const items = useNests()
  const nest = items.find((n) => n.id === id) ?? null
  const patch = React.useCallback(async (p: Partial<Nest>): Promise<boolean> => {
    if (!id) return false
    const o = await taskingActions.updateNest(id, p)
    if (!o.ok) notify(`nests:patch:${id}`, 'warning', errText(o))
    return o.ok
  }, [id])
  const back = <Button size="xs" variant="ghost" onClick={() => entities.back()}><Icon icon="chev.left" data-icon="inline-start" />{t('nests.back')}</Button>
  if (!id || !nest) return <div className="flex flex-col gap-2">{back}<PanelEmpty brand={false} title={t('nests.none')} /></div>
  const kind = nest.kind ?? 'box'
  const ch0 = nest.chargers?.[0]
  const pk0 = nest.energy?.packs?.[0]
  const remove = () => confirmThen({
    id: 'nest.remove', title: t('nests.remove.title', { name: sanitizeText(nest.name ?? id, 32) }), body: t('nests.remove.body'), action: t('nests.remove'),
    run: () => void taskingActions.removeNest(id).then((o) => {
      if (o.ok) entities.openNest(null)
      else notify('nests:remove', 'warning', o.code === 105 ? t('nests.removeBusy') : errText(o))
    }),
  })
  return (
    <div className="flex min-h-0 flex-col gap-3 overflow-y-auto" data-nest-detail={id}>
      <div className="flex items-center gap-1.5">
        {back}
        <Icon icon={NEST_ICON[kind]} label={t(`nest.kind.${kind}`)} />
        <span className="truncate font-medium">{sanitizeText(nest.name ?? id, 32)}</span>
        <span className="shrink-0 text-hud-sub text-muted-foreground">{t(`nest.kind.${kind}`)}</span>
        {canEdit ? <Button size="xs" variant="ghost" className="ml-auto" onClick={remove} data-nest-remove="">{t('nests.remove')}</Button> : null}
      </div>
      {nest.crewed ? <p className="text-hud-sub text-muted-foreground" data-nest-crewed="">{t('nests.crewedNote')}</p> : null}
      {nest.pos[0] !== null ? <p className="font-mono text-hud-sub text-muted-foreground">{`E ${fmt.num(Number(nest.pos[0]), 1)} N ${fmt.num(Number(nest.pos[1]), 1)}`}</p> : null}
      <NestStatusView id={id} nest={nest} />
      {canEdit ? (
      <div className="grid grid-cols-2 gap-2" data-nest-form="">
        <div className="col-span-2"><TextField id="nest-name" label={t('nests.f.name')} value={nest.name ?? ''} validate={labelOk} onCommit={(v) => patch({ name: v })} /></div>
        <NumField id="nest-slots" label={t('nests.f.slots')} value={nest.slots} range={{ min: 1, max: 12 }} digits={0} onCommit={(v) => patch({ slots: v })} />
        <Field>
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('nests.f.energy')}</FieldLabel>
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[nest.energy?.mode ?? 'swap']} aria-label={t('nests.f.energy')}
            onValueChange={(v: unknown[]) => (v[0] === 'swap' || v[0] === 'charge') && void patch({ energy: { ...(nest.energy ?? {}), mode: v[0] } })}>
            <ToggleGroupItem value="swap">{t('nest.energy.swap')}</ToggleGroupItem>
            <ToggleGroupItem value="charge">{t('nest.energy.charge')}</ToggleGroupItem>
          </ToggleGroup>
        </Field>
        <NumField id="nest-swap" label={t('nests.f.swap')} value={nest.energy?.swap_s} unit="s" range={{ min: 10, max: 3600 }} digits={0}
          onCommit={(v) => patch({ energy: { ...(nest.energy ?? { mode: 'swap' }), swap_s: v } })} />
        <NumField id="nest-turn" label={t('nests.f.turnaround')} value={nest.turnaround_s} unit="s" range={{ min: 0, max: 3600 }} digits={0} onCommit={(v) => patch({ turnaround_s: v })} />
        <NumField id="nest-launch" label={t('nests.f.launchInterval')} value={nest.launch_interval_s} unit="s" range={{ min: 0, max: 600 }} digits={0} onCommit={(v) => patch({ launch_interval_s: v })} />
        <NumField id="nest-wind" label={t('nests.f.wind')} value={nest.wind_max_mps} unit="m/s" range={{ min: 0, max: 40 }} digits={1} onCommit={(v) => patch({ wind_max_mps: v })} />
        <NumField id="nest-link" label={t('nests.f.link')} value={nest.link_range_m ?? null} unit="m" range={{ min: 100, max: 50000 }} digits={0} onCommit={(v) => patch({ link_range_m: v })} />
        <Field orientation="horizontal">
          <Switch id="nest-rtk" checked={nest.rtk_base === true} onCheckedChange={(on: boolean) => void patch({ rtk_base: on })} />
          <FieldLabel htmlFor="nest-rtk" className="font-normal">{t('nests.f.rtk')}</FieldLabel>
        </Field>
        {ch0 ? (
          <>
            <NumField id="nest-ch-count" label={t('nests.f.chargers')} value={ch0.count} range={{ min: 0, max: 16 }} digits={0}
              onCommit={(v) => patch({ chargers: [{ ...ch0, count: v }, ...(nest.chargers ?? []).slice(1)] })} />
            <NumField id="nest-ch-cells" label={t('nests.f.cellsMax')} value={ch0.cells_max} range={{ min: 1, max: 14 }} digits={0}
              onCommit={(v) => patch({ chargers: [{ ...ch0, cells_max: v }, ...(nest.chargers ?? []).slice(1)] })} />
          </>
        ) : null}
        {pk0 ? (
          <>
            <NumField id="nest-pk-cells" label={t('nests.f.packCells')} value={pk0.cells} range={{ min: 1, max: 14 }} digits={0}
              onCommit={(v) => patch({ energy: { ...(nest.energy ?? { mode: 'swap' }), packs: [{ ...pk0, cells: v }, ...(nest.energy?.packs ?? []).slice(1)] } })} />
            <NumField id="nest-pk-count" label={t('nests.f.packCount')} value={pk0.count} range={{ min: 0, max: 40 }} digits={0}
              onCommit={(v) => patch({ energy: { ...(nest.energy ?? { mode: 'swap' }), packs: [{ ...pk0, count: v }, ...(nest.energy?.packs ?? []).slice(1)] } })} />
          </>
        ) : null}
      </div>
      ) : null}
      {chargerMismatch(nest) ? <p className="text-hud-sub" data-nest-mismatch="">{t('nests.cellsMismatch')}</p> : null}
    </div>
  )
}
