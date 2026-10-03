// Route and area editor, the DroneRail page 3 `mission-edit` (M15-FR-022; AWR-14 §5.3 D1-ext, §6.8; UX-FR-023, UX-FR-024;
// D1-AC-17). Header: back (AlertDialog "discard changes?" while DIRTY), title and the target vehicle Badge. Tools
// (ToggleGroup with the sliding indicator): select, add waypoint, draw area; undo and redo (ButtonGroup, Mod+Z,
// Mod+Shift+Z). Count line "waypoints 12 / 1000 · 3.4 / 20 km" with the coarse check state. Route: speed InputGroup
// (empty = cruise speed), the reference of new waypoints (NativeSelect AGL or world z), the waypoint table (lieflat
// LfTable, virtualised above 200 rows; inline InputGroup cells for E, N and height, NativeSelect for the reference, a
// warning icon on the rows that start a rejected segment, row menu insert, move up, move down, delete), discard and
// submit. Area: the drawing hint, generator Select (coverage = lawnmower, search = expanding square), AGL, spacing, speed,
// assigned vehicles Combobox (chips), preview (energy pre-check marks infeasible vehicles), create. Every text comes from
// the i18n files; the viewport side (handles, clicks, drags) is EditViewportLayer.
import * as React from 'react'
import { t as tStatic, useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { rtClient } from '@/net/rt'
import { Alert, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { ButtonGroup } from '@/ui/components/ui/button-group'
import { Combobox, ComboboxChip, ComboboxChips, ComboboxChipsInput, ComboboxContent, ComboboxEmpty, ComboboxItem, ComboboxList, ComboboxValue, useComboboxAnchor } from '@/ui/components/ui/combobox'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { Field, FieldGroup, FieldLabel } from '@/ui/components/ui/field'
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from '@/ui/components/ui/input-group'
import { Kbd } from '@/ui/components/ui/kbd'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Spinner } from '@/ui/components/ui/spinner'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { comboLabel } from '@/ui/hotkeys/registry'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { PanelEmpty } from '@/ui/brand'
import { confirmThen } from '@/ui/actions/ConfirmHost'
import { useConnView } from '@/ui/shell/connView'
import { writeDeniedKey } from '@/ui/shell/guards'
import { prefs, prefsStore } from '@/stores/prefs'
import { useMission } from '@/stores/mission'
import { ROUTE_LIMITS, type AltRef, type AreaGenerator, type DraftWp } from './editModel'
import { areaEditor, draftLengthM, editStore, routeEditor, setOnRouteRunning, submitBlockedKey, useEdit, type EditTool } from './editStore'

const TOOLS: readonly { id: EditTool; icon: 'tool.select' | 'wp.add' | 'tool.box'; key: string }[] = [
  { id: 'select', icon: 'tool.select', key: 'edit.tool.select' },
  { id: 'add', icon: 'wp.add', key: 'edit.tool.add' },
  { id: 'area', icon: 'tool.box', key: 'edit.tool.area' },
]
const GENERATORS: readonly AreaGenerator[] = ['lawnmower', 'expanding_square']

/** open the editor for a vehicle on page 3 of the right rail (detail page "edit route", mission tab buttons) */
export function openEditor(vehicle: string, tool: EditTool = 'select'): void {
  routeEditor.open(vehicle, tool)
  // the edit layout of AWR-14 §3.5: the right rail at 400 px while the editor is open (restored when it closes)
  const w = prefsStore.getState().layout.right.width
  if (w < EDIT_RAIL_PX) widthBeforeEdit = w
  prefs.setLayout({ right: { open: true, page: 'edit', width: Math.max(w, EDIT_RAIL_PX) } })
}
const EDIT_RAIL_PX = 400
let widthBeforeEdit: number | null = null
setOnRouteRunning(() => restoreRailWidth())
/** the rail width from before the editor widened it (once) */
export function restoreRailWidth(): void {
  if (widthBeforeEdit === null) return
  const w = widthBeforeEdit
  widthBeforeEdit = null
  if (prefsStore.getState().layout.right.width === EDIT_RAIL_PX) prefs.setLayout({ right: { width: w } })
}

/** the result of the last submitted route of a vehicle (detail page and mission tab) */
export function RouteStatus({ vehicle }: { vehicle: string }) {
  const t = useT()
  const last = useEdit((s) => (s.lastRoute?.vehicle === vehicle ? s.lastRoute : null))
  if (!last) return null
  const busy = last.status === 'sent' || last.status === 'accepted' || last.status === 'running' || last.status === 'converting'
  const bad = last.status === 'failed' || last.status === 'rejected'
  return (
    <p className="flex items-center gap-1.5 text-hud-sub text-muted-foreground" data-route-status={last.status} data-route-vehicle={vehicle}>
      {busy ? <Spinner className="size-3" /> : <Icon icon={bad ? 'alert.warning' : 'check'} />}
      {t('edit.routeStatus', { state: t(`edit.submit.${last.status}`) })}
    </p>
  )
}

/** leave the editor: back to the vehicle detail page; a DIRTY draft asks first (AWR-14 §6.8 "discard changes?") */
export function leaveEditor(): void {
  const s = editStore.getState()
  const go = () => {
    routeEditor.close()
    prefs.setLayout({ right: { page: 'detail' } })
    restoreRailWidth()
  }
  if (s.phase !== 'DIRTY') {
    go()
    return
  }
  confirmThen({ id: 'edit.discard', title: tStatic('edit.discardTitle'), body: tStatic('edit.discardBody'), action: tStatic('edit.discard'), run: go })
}

/** a number cell: local text while typing, committed on Enter or blur, Escape reverts */
function NumCell({ value, label, step, onCommit, width = 'w-16', suffix }: { value: number; label: string; step: number; onCommit: (v: number) => void; width?: string; suffix?: string }) {
  // a number input takes ASCII digits and '-' only (fmt.num writes the typographic minus U+2212)
  const shown = Number.isFinite(value) ? value.toFixed(1) : ''
  const [text, setText] = React.useState<string | null>(null)
  const commit = () => {
    if (text === null) return
    const v = Number(text)
    setText(null)
    if (Number.isFinite(v) && text.trim() !== '' && v.toFixed(1) !== shown) onCommit(v)
  }
  return (
    <InputGroup className={cn('h-6', width)}>
      <InputGroupInput type="number" inputMode="decimal" step={step} aria-label={label} value={text ?? shown} className="px-1 text-right font-mono tabular-nums"
        onChange={(e) => setText(e.target.value)} onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commit()
          } else if (e.key === 'Escape') setText(null)
        }} />
      {suffix ? <InputGroupAddon align="inline-end"><InputGroupText>{suffix}</InputGroupText></InputGroupAddon> : null}
    </InputGroup>
  )
}

function RowMenu({ i, n }: { i: number; n: number }) {
  const t = useT()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="icon-xs" variant="ghost" aria-label={t('edit.row.menu', { n: i + 1 })} data-wp-menu={i} />}>
        <Icon icon="more" />
      </DropdownMenuTrigger>
      {/* React events bubble out of the portal to the table row: without this the row click selected waypoint i again
          after "insert" had selected the new one */}
      <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t('edit.row.title', { n: i + 1 })}</DropdownMenuLabel>
          <DropdownMenuItem disabled={n >= ROUTE_LIMITS.maxWaypoints} onClick={() => routeEditor.insertAfter(i)} data-wp-insert={i}><Icon icon="plus" />{t('edit.row.insert')}</DropdownMenuItem>
          <DropdownMenuItem disabled={i === 0} onClick={() => routeEditor.move(i, -1)} data-wp-up={i}><Icon icon="chev.up" />{t('edit.row.up')}</DropdownMenuItem>
          <DropdownMenuItem disabled={i >= n - 1} onClick={() => routeEditor.move(i, 1)} data-wp-down={i}><Icon icon="chev.down" />{t('edit.row.down')}</DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem onClick={() => routeEditor.remove(i)} data-wp-delete={i}><Icon icon="wp.remove" />{t('edit.row.delete')}</DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

interface Row { i: number; w: DraftWp; bad: string | null }

function RouteTable() {
  const t = useT()
  const wps = useEdit((s) => s.wps)
  const sel = useEdit((s) => s.sel)
  const violations = useEdit((s) => s.check.violations)
  const rows = React.useMemo<Row[]>(() => {
    const bad = new Map<number, string>()
    for (const v of violations) bad.set(v.seg, v.reason)
    return wps.map((w, i) => ({ i, w, bad: bad.get(i) ?? null }))
  }, [wps, violations])
  const columns: LfColumn<Row>[] = [
    { key: 'n', label: '#', width: '1.75rem', format: (r) => <span className="font-mono tabular-nums">{r.i + 1}</span> },
    { key: 'e', label: 'E', unit: 'm', align: 'right', format: (r) => <NumCell value={r.w.x} step={1} width="w-14" label={t('edit.col.eOf', { n: r.i + 1 })} onCommit={(v) => routeEditor.update(r.i, { x: v })} /> },
    { key: 'nn', label: 'N', unit: 'm', align: 'right', format: (r) => <NumCell value={r.w.y} step={1} width="w-14" label={t('edit.col.nOf', { n: r.i + 1 })} onCommit={(v) => routeEditor.update(r.i, { y: v })} /> },
    { key: 'h', label: t('edit.col.h'), unit: 'm', align: 'right', format: (r) => <NumCell value={r.w.h} step={1} width="w-14" label={t('edit.col.hOf', { n: r.i + 1 })} onCommit={(v) => routeEditor.update(r.i, { h: v })} /> },
    { key: 'ref', label: t('edit.col.ref'), format: (r) => (
      <NativeSelect size="sm" value={r.w.ref} aria-label={t('edit.col.refOf', { n: r.i + 1 })} onChange={(e) => routeEditor.setWpRef(r.i, e.target.value as AltRef)}>
        <NativeSelectOption value="AGL">{t('edit.refShort.AGL')}</NativeSelectOption>
        <NativeSelectOption value="WORLD">{t('edit.refShort.WORLD')}</NativeSelectOption>
      </NativeSelect>
    ) },
    { key: 'warn', label: '', width: '1.25rem', format: (r) => (r.bad ? (
      <Tooltip>
        <TooltipTrigger render={<span className="inline-flex text-brand-text" data-wp-violation={r.i} />}><Icon icon="alert.warning" label={t('edit.violation')} /></TooltipTrigger>
        <TooltipContent>{t('edit.violationOf', { reason: t(`edit.reason.${r.bad}`) })}</TooltipContent>
      </Tooltip>
    ) : null) },
    { key: 'menu', label: '', width: '1.75rem', format: (r) => <RowMenu i={r.i} n={rows.length} /> },
  ]
  if (!rows.length) return <PanelEmpty brand={false} title={t('edit.route.empty')} description={t('edit.route.emptyHint')} />
  return (
    <LfTable columns={columns} rows={rows} rowKey={(r) => String(r.w.id)} selected={sel >= 0 && wps[sel] ? String(wps[sel].id) : null}
      onRowClick={(r) => routeEditor.select(r.i)} figureId="waypoint-table" ariaLabel={t('edit.route.table')} height={300} rowHeight={32}
      className="[&_td]:px-1 [&_th]:px-1" />
  )
}

function CountLine() {
  const t = useT()
  const n = useEdit((s) => s.wps.length)
  const lenKm = useEdit((s) => draftLengthM(s) / 1000)
  const check = useEdit((s) => s.check)
  return (
    <div className="flex items-center justify-between gap-2 text-hud-sub" data-route-count={n}>
      <span className="font-mono tabular-nums">{t('edit.count', { n: fmt.count(n), max: fmt.count(ROUTE_LIMITS.maxWaypoints), km: fmt.num(lenKm, 2), maxKm: fmt.num(ROUTE_LIMITS.maxLengthM / 1000) })}</span>
      <span className="inline-flex items-center gap-1 text-muted-foreground" data-route-check={check.pending ? 'pending' : check.violations.length ? 'violations' : check.ok ? 'ok' : 'none'}>
        {check.pending ? <><Spinner className="size-3" />{t('edit.check.pending')}</>
          : check.violations.length ? <><Icon icon="alert.warning" className="text-brand-text" />{t('edit.check.violations', { n: check.violations.length })}</>
          : check.ok ? <><Icon icon="check" />{t('edit.check.ok')}</> : null}
      </span>
    </div>
  )
}

function RouteFooter() {
  const t = useT()
  const s = useEdit((x) => x)
  useConnView((x) => x.version)
  const denied = writeDeniedKey()
  const blocked = denied ?? submitBlockedKey(s)
  const status = s.submit.status
  return (
    <div className="flex flex-col gap-1.5">
      {status !== 'idle' ? (
        <p className="flex items-center gap-1.5 text-hud-sub text-muted-foreground" data-route-submit={status}>
          {status === 'converting' || status === 'sent' || status === 'accepted' ? <Spinner className="size-3" /> : <Icon icon={status === 'failed' || status === 'rejected' ? 'alert.warning' : 'check'} />}
          {t(`edit.submit.${status}`)}{s.submit.reason ? ` · ${s.submit.reason}` : ''}
        </p>
      ) : null}
      <div className="flex flex-wrap justify-end gap-2">
        <Button size="sm" variant="outline" onClick={leaveEditor} data-route-discard="">{t('edit.discard')}</Button>
        <Tooltip>
          <TooltipTrigger render={<span className="inline-flex" />}>
            <Button size="sm" disabled={blocked !== null} onClick={() => void routeEditor.submit()} data-route-submit-button="">
              {s.phase === 'SUBMITTING' ? <Spinner className="size-3.5" data-icon="inline-start" /> : <Icon icon="cmd.followpath" data-icon="inline-start" />}
              {t('edit.submit')}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{blocked ? t(blocked, { n: s.check.violations.length }) : <>{t('edit.submitHint')}<Kbd>Enter</Kbd></>}</TooltipContent>
        </Tooltip>
      </div>
    </div>
  )
}

function RouteSection() {
  const t = useT()
  const speed = useEdit((s) => s.speedMps)
  const ref = useEdit((s) => s.ref)
  const [speedText, setSpeedText] = React.useState(speed === null ? '' : String(speed))
  const sp = Number(speedText)
  const speedOk = speedText === '' || (Number.isFinite(sp) && sp > 0 && sp <= 12)
  return (
    <div className="flex min-h-0 flex-col gap-2" data-route-section="">
      <div className="grid grid-cols-2 gap-2">
        <Field>
          <FieldLabel htmlFor="route-speed" className="text-hud-cap uppercase text-muted-foreground">{t('edit.speed')}</FieldLabel>
          <InputGroup className="h-7">
            <InputGroupInput id="route-speed" type="number" inputMode="decimal" min={0.5} max={12} step={0.5} value={speedText} placeholder={t('edit.speedDefault')}
              aria-invalid={!speedOk} onChange={(e) => {
                setSpeedText(e.target.value)
                const v = Number(e.target.value)
                routeEditor.setSpeed(e.target.value === '' || !(v > 0 && v <= 12) ? null : v)
              }} />
            <InputGroupAddon align="inline-end"><InputGroupText>m/s</InputGroupText></InputGroupAddon>
          </InputGroup>
        </Field>
        <Field>
          <FieldLabel htmlFor="route-ref" className="text-hud-cap uppercase text-muted-foreground">{t('edit.newRef')}</FieldLabel>
          <NativeSelect id="route-ref" size="sm" className="w-full" value={ref} onChange={(e) => routeEditor.setRef(e.target.value as AltRef)}>
            <NativeSelectOption value="AGL">{t('edit.ref.AGL')}</NativeSelectOption>
            <NativeSelectOption value="WORLD">{t('edit.ref.WORLD')}</NativeSelectOption>
          </NativeSelect>
        </Field>
      </div>
      <RouteTable />
      <RouteFooter />
    </div>
  )
}

function VehiclesField() {
  const t = useT()
  const anchor = useComboboxAnchor()
  const chosen = useEdit((s) => s.gen.vehicles)
  const infeasible = useEdit((s) => s.preview.infeasible)
  const rt = rtClient()
  const rosterVersion = rt?.roster.version ?? 0
  // oxlint-disable-next-line react-hooks/exhaustive-deps -- the roster is read in place; its version is the cache key
  const items = React.useMemo(() => (rt ? rt.roster.entries().map((e) => e.id) : [...chosen]), [rt, rosterVersion, chosen])
  return (
    <Field>
      <FieldLabel className="text-hud-cap uppercase text-muted-foreground">{t('edit.area.vehicles')}</FieldLabel>
      <Combobox multiple autoHighlight items={items} value={[...chosen]} onValueChange={(v: unknown) => areaEditor.setGen({ vehicles: Array.isArray(v) ? (v as string[]) : [] })}>
        <ComboboxChips ref={anchor} data-area-vehicles="">
          <ComboboxValue>
            {(values: string[]) => (
              <React.Fragment>
                {values.map((v) => (
                  <ComboboxChip key={v} className={cn('font-mono', infeasible.includes(v) && 'ring-1 ring-brand')} data-infeasible={infeasible.includes(v) ? '' : undefined}>{v}</ComboboxChip>
                ))}
                <ComboboxChipsInput aria-label={t('edit.area.vehicles')} />
              </React.Fragment>
            )}
          </ComboboxValue>
        </ComboboxChips>
        <ComboboxContent anchor={anchor}>
          <ComboboxEmpty>{t('edit.area.noVehicle')}</ComboboxEmpty>
          <ComboboxList>{(id: string) => <ComboboxItem key={id} value={id} className="font-mono">{id}</ComboboxItem>}</ComboboxList>
        </ComboboxContent>
      </Combobox>
      {infeasible.length ? <p className="text-hud-sub text-brand-text" data-energy-infeasible="">{t('edit.area.energy', { ids: infeasible.join(', ') })}</p> : null}
    </Field>
  )
}

function AreaSection() {
  const t = useT()
  const area = useEdit((s) => s.area)
  const gen = useEdit((s) => s.gen)
  const preview = useEdit((s) => s.preview)
  const created = useEdit((s) => s.created)
  const createdRow = useMission((s) => (created?.mid ? s.rows.get(created.mid) : undefined))
  useConnView((x) => x.version)
  const denied = writeDeniedKey()
  const genItems = GENERATORS.map((g) => ({ value: g, label: t(`edit.gen.${g}`) }))
  const num = (k: 'aglM' | 'spacingM' | 'speedMps', label: string, unit: string, min: number, max: number, step: number) => (
    <Field>
      <FieldLabel htmlFor={`area-${k}`} className="text-hud-cap uppercase text-muted-foreground">{label}</FieldLabel>
      <InputGroup className="h-7">
        <InputGroupInput id={`area-${k}`} type="number" inputMode="decimal" min={min} max={max} step={step} value={String(gen[k])}
          onChange={(e) => {
            const v = Number(e.target.value)
            if (Number.isFinite(v) && v >= min && v <= max) areaEditor.setGen({ [k]: v })
          }} />
        <InputGroupAddon align="inline-end"><InputGroupText>{unit}</InputGroupText></InputGroupAddon>
      </InputGroup>
    </Field>
  )
  const canPreview = area.closed && gen.vehicles.length > 0 && preview.status !== 'pending'
  return (
    <div className="flex min-h-0 flex-col gap-2" data-area-section="" data-area-closed={area.closed ? '' : undefined} data-area-vertices={area.pts.length}>
      <Alert className="py-1.5">
        <Icon icon="tool.box" />
        <AlertDescription className="text-hud-sub">{area.closed ? t('edit.area.closed', { n: area.pts.length }) : t('edit.area.hint')}</AlertDescription>
      </Alert>
      {area.error ? <p className="text-hud-sub text-brand-text" data-area-error={area.error}>{t(area.error)}</p> : null}
      {area.closed ? (
        <FieldGroup className="gap-2">
          <Field>
            <FieldLabel className="text-hud-cap uppercase text-muted-foreground">{t('edit.area.generator')}</FieldLabel>
            <Select items={genItems} value={gen.generator} onValueChange={(v) => v && areaEditor.setGen({ generator: v as AreaGenerator })}>
              <SelectTrigger size="sm" className="w-full" aria-label={t('edit.area.generator')} data-area-generator=""><SelectValue /></SelectTrigger>
              <SelectContent>{genItems.map((g) => <SelectItem key={g.value} value={g.value}>{g.label}</SelectItem>)}</SelectContent>
            </Select>
          </Field>
          <div className="grid grid-cols-3 gap-2">
            {num('aglM', t('edit.area.agl'), 'm', 5, 150, 5)}
            {num('spacingM', t(gen.generator === 'lawnmower' ? 'edit.area.spacing' : 'edit.area.leg'), 'm', 5, 200, 5)}
            {num('speedMps', t('edit.speed'), 'm/s', 1, 12, 0.5)}
          </div>
          <VehiclesField />
        </FieldGroup>
      ) : null}
      {preview.status === 'ok' ? (
        <p className="text-hud-sub text-muted-foreground" data-area-preview="ok">
          {t('edit.area.previewOk', { n: preview.paths.length, km: fmt.num(preview.paths.reduce((a, p) => a + p.lengthM, 0) / 1000, 2) })}
          {preview.coveragePred !== null ? ` · ${t('edit.area.coverage', { pct: fmt.pct(preview.coveragePred * 100) })}` : ''}
        </p>
      ) : preview.status === 'error' ? <p className="text-hud-sub text-brand-text" data-area-preview="error">{t('edit.area.previewFailed', { reason: preview.error ?? '' })}</p> : null}
      {created ? (
        <p className="flex items-center gap-1.5 text-hud-sub text-muted-foreground" data-area-created={created.status} data-area-mid={created.mid ?? ''} data-mission-state={createdRow?.state ?? ''}>
          {created.status === 'creating' || created.status === 'starting' ? <Spinner className="size-3" /> : <Icon icon={created.status === 'error' ? 'alert.warning' : 'mission.running'} />}
          {created.status === 'error' ? t('edit.area.createFailed', { reason: created.error ?? '' })
            : t('edit.area.created', { mid: created.mid ?? '', state: createdRow ? t(`mission.state.${createdRow.state}`) : t('edit.area.starting') })}
        </p>
      ) : null}
      <div className="flex flex-wrap justify-end gap-2">
        <Button size="sm" variant="outline" disabled={!area.pts.length} onClick={() => areaEditor.clear()} data-area-clear="">{t('edit.area.clear')}</Button>
        {!area.closed ? (
          <Button size="sm" variant="outline" disabled={area.pts.length < 3} onClick={() => areaEditor.close()} data-area-close="">{t('edit.area.close')}</Button>
        ) : (
          <Button size="sm" variant="outline" disabled={!canPreview} onClick={() => void areaEditor.preview()} data-area-preview-button="">
            {preview.status === 'pending' ? <Spinner className="size-3.5" data-icon="inline-start" /> : <Icon icon="layer.visible" data-icon="inline-start" />}
            {t('edit.area.preview')}
          </Button>
        )}
        <Tooltip>
          <TooltipTrigger render={<span className="inline-flex" />}>
            <Button size="sm" disabled={denied !== null || !area.closed || !gen.vehicles.length || created?.status === 'creating'} onClick={() => void areaEditor.create()} data-area-create="">
              <Icon icon="cmd.coverage" data-icon="inline-start" />
              {t('edit.area.create')}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{denied ? t(denied) : t('edit.area.createHint')}</TooltipContent>
        </Tooltip>
      </div>
    </div>
  )
}

export function MissionEditPanel() {
  const t = useT()
  const phase = useEdit((s) => s.phase)
  const vehicle = useEdit((s) => s.vehicle)
  const tool = useEdit((s) => s.tool)
  const canUndo = useEdit((s) => s.history.past.length > 0 && s.phase !== 'SUBMITTING')
  const canRedo = useEdit((s) => s.history.future.length > 0 && s.phase !== 'SUBMITTING')
  if (phase === 'CLOSED') {
    return (
      <PanelEmpty brand={false} title={t('edit.closed')} description={t('edit.closedHint')}
        action={<Button size="sm" variant="outline" onClick={() => prefs.setLayout({ right: { page: 'detail' } })}>{t('edit.back')}</Button>} />
    )
  }
  return (
    <div className="flex min-h-0 flex-col gap-2" data-mission-edit="" data-edit-phase={phase} data-edit-tool={tool}>
      <div className="flex items-center gap-1.5">
        <Button size="icon-sm" variant="ghost" aria-label={t('edit.back')} onClick={leaveEditor} data-edit-back=""><Icon icon="chev.left" /></Button>
        <span className="min-w-0 flex-1 truncate text-hud-title font-semibold">{t('edit.title')}</span>
        <Badge variant="outline" className="font-mono">{vehicle}</Badge>
      </div>
      <div className="flex items-center justify-between gap-2">
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[tool]} aria-label={t('edit.tools')}
          onValueChange={(v: unknown[]) => typeof v[0] === 'string' && routeEditor.setTool(v[0] as EditTool)}>
          {TOOLS.map((x) => (
            <Tooltip key={x.id}>
              <TooltipTrigger render={<ToggleGroupItem value={x.id} aria-label={t(x.key)} data-edit-tool-item={x.id} />}>
                <Icon icon={x.icon} />
              </TooltipTrigger>
              <TooltipContent>{t(x.key)}</TooltipContent>
            </Tooltip>
          ))}
        </ToggleGroup>
        <ButtonGroup>
          <Tooltip>
            <TooltipTrigger render={<Button size="icon-sm" variant="outline" disabled={!canUndo} aria-label={t('edit.undo')} onClick={() => routeEditor.undo()} data-edit-undo="" />}>
              <Icon icon="edit.undo" />
            </TooltipTrigger>
            <TooltipContent>{t('edit.undo')}<Kbd>{comboLabel('mod+KeyZ').join(' ')}</Kbd></TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger render={<Button size="icon-sm" variant="outline" disabled={!canRedo} aria-label={t('edit.redo')} onClick={() => routeEditor.redo()} data-edit-redo="" />}>
              <Icon icon="edit.redo" />
            </TooltipTrigger>
            <TooltipContent>{t('edit.redo')}<Kbd>{comboLabel('mod+shift+KeyZ').join(' ')}</Kbd></TooltipContent>
          </Tooltip>
        </ButtonGroup>
      </div>
      <CountLine />
      {tool === 'area' ? <AreaSection /> : <RouteSection />}
    </div>
  )
}
