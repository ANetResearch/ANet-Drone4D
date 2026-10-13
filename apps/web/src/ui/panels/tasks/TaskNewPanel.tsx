// "New task" page, right rail page 3 `task-new` (R-D2-03 to R-D2-06, R-D2-16, R-D2-19; M15-FR-214; UX-FR-126 to UX-FR-129;
// AWR-14 §20.9.1 to §20.9.3): the type ToggleGroup (seven types; choosing one starts its drawing tool in the viewport), the
// drawing hint with the live point count and area, the parameters of the type (UI prefill of §20.9.1; patrols show the
// required GSD read-only, the GSD scan links the calculator), the "more" section (priority, detection action, allowed
// nests), the front-end pre-check (§20.9.2, blocking issues disable the preview), "preview allocation" (dry-run through
// stores/tasking.ts, drawn by the planning layer; the result card is TaskPreviewCard) and "start task" (create with the
// preview's parameters, the server reuses the preview within 60 s). The page is the operator's (canEdit); the shared show
// and viewers never see it. Store writes happen on user actions only.
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { cn } from '@/lib/utils'
import { taskingActions, useTasking, type TaskType } from '@/stores/tasking'
import { useTargetList } from '@/ui/panels/targets/TargetsPanel'
import { Alert, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Checkbox } from '@/ui/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/ui/components/ui/collapsible'
import { Field, FieldDescription, FieldLabel } from '@/ui/components/ui/field'
import { InputGroup, InputGroupInput } from '@/ui/components/ui/input-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Spinner } from '@/ui/components/ui/spinner'
import { Switch } from '@/ui/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { NumField } from '@/ui/sandbox/fields'
import { d2Tools, DRAW_MAX_POINTS, useD2Tool } from '@/ui/tools/d2Tools'
import { gsdCalc } from '@/ui/views/GsdCalculator'
import { TARGET_CLASSES, type TargetClassName } from '@/app/shell/engineD2'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { taskDraft, useTaskDraft } from './taskDraft'
import { TaskPreviewCard } from './TaskPreviewCard'
import {
  areaM2, blocking, buildSpec, drawKindOf, lineLengthM, precheck, requiredGsdM, TASK_ICON, TASK_TYPE_ORDER, type Level, type TaskForm,
} from './taskModel'

const LEVELS: readonly Level[] = ['D', 'R', 'I']
const yes = (): Promise<boolean> => Promise.resolve(true)

/** the drawing hint with the live point count, area or length (AWR-14 §20.9.2) */
function DrawHint({ type }: { type: TaskType }) {
  const t = useT()
  const pts = useD2Tool((s) => s.points)
  const mode = useD2Tool((s) => s.mode)
  const kind = drawKindOf(type)
  const xy = pts.map((p) => [p[0], p[1]] as const)
  const stat = kind === 'area'
    ? t('task.draw.areaStat', { n: pts.length, max: DRAW_MAX_POINTS, km2: fmt.num(areaM2(xy) / 1e6, 3) })
    : kind === 'line' ? t('task.draw.lineStat', { n: pts.length, max: DRAW_MAX_POINTS, km: fmt.num(lineLengthM(xy) / 1000, 2) }) : null
  return (
    <Alert data-task-drawing={kind}>
      <Icon icon={TASK_ICON[type]} />
      <AlertDescription className="flex flex-col gap-1.5">
        <span>{t(`task.draw.hint.${kind}`)}</span>
        {stat ? <span className="font-mono text-hud-sub" data-task-draw-stat={pts.length}>{stat}</span> : null}
        <span className="flex gap-1.5">
          {kind === 'line' || kind === 'area' ? (
            <Button size="xs" variant="outline" disabled={!mode || pts.length < (kind === 'area' ? 3 : 2)} onClick={() => d2Tools.commit()} data-task-draw-done="">
              {t('task.draw.done')}
            </Button>
          ) : null}
          {pts.length ? <Button size="xs" variant="ghost" onClick={() => d2Tools.undo()}>{t('tool.d2.undo')}</Button> : null}
          <Button size="xs" variant="ghost" className="ml-auto" onClick={() => taskDraft.reset()}>{t('common.cancel')}</Button>
        </span>
      </AlertDescription>
    </Alert>
  )
}

/** one-line description of the drawn geometry with "redraw" */
function GeomLine({ type }: { type: TaskType }) {
  const t = useT()
  const g = useTaskDraft((s) => s.geom)
  const targets = useTargetList()
  const kind = drawKindOf(type)
  if (kind === 'target') {
    return (
      <Field>
        <FieldLabel className="text-hud-sub text-muted-foreground">{t('task.f.target')}</FieldLabel>
        <div className="flex items-center gap-1.5">
          <Select items={targets.map((x) => ({ value: x.id, label: x.label }))} value={g.target ?? ''} onValueChange={(v) => typeof v === 'string' && taskDraft.setTarget(v)}>
            <SelectTrigger size="sm" className="min-w-0 flex-1" data-task-target=""><SelectValue placeholder={t('task.f.targetPick')} /></SelectTrigger>
            <SelectContent>
              {targets.map((x) => <SelectItem key={x.id} value={x.id}>{`${sanitizeText(x.label, 24)} · ${t(`target.class.${x.cls ?? 'object'}`)}`}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button size="xs" variant="outline" onClick={() => taskDraft.draw()} data-task-pick-map="">{t('task.f.targetOnMap')}</Button>
        </div>
        {targets.length === 0 ? <FieldDescription>{t('task.f.noTargets')}</FieldDescription> : null}
      </Field>
    )
  }
  const xy = g.points.map((p) => [p[0], p[1]] as const)
  const text = kind === 'area' ? t('task.geom.area', { n: xy.length, km2: fmt.num(areaM2(xy) / 1e6, 3) })
    : kind === 'line' ? t('task.geom.line', { n: xy.length, km: fmt.num(lineLengthM(xy) / 1000, 2) })
      : g.points[0] ? t('task.geom.point', { e: fmt.num(g.points[0][0], 1), n: fmt.num(g.points[0][1], 1) }) : ''
  return (
    <div className="flex items-center gap-2 text-hud-sub">
      <Icon icon={TASK_ICON[type]} />
      <span className="font-mono" data-task-geom={xy.length}>{text}</span>
      <Button size="xs" variant="ghost" className="ml-auto" onClick={() => taskDraft.draw()} data-task-redraw="">{t('task.draw.again')}</Button>
    </div>
  )
}

function Toggle2<V extends string>({ label, value, items, onChange, id }: { label: string; value: V; items: readonly { v: V; label: string }[]; onChange: (v: V) => void; id: string }) {
  return (
    <Field className={items.length > 3 ? 'col-span-2 min-w-0' : 'min-w-0'}>
      <FieldLabel className="text-hud-sub text-muted-foreground">{label}</FieldLabel>
      <ToggleGroup spacing={0} size="sm" variant="outline" value={[value]} aria-label={label} data-task-toggle={id} className="w-full"
        onValueChange={(v: unknown[]) => items.some((x) => x.v === v[0]) && onChange(v[0] as V)}>
        {items.map((x) => <ToggleGroupItem key={x.v} value={x.v} className="min-w-0 flex-1 px-1">{x.label}</ToggleGroupItem>)}
      </ToggleGroup>
    </Field>
  )
}

/** "auto or k" vehicle count */
function VehiclesField({ f }: { f: TaskForm }) {
  const t = useT()
  const auto = f.nVehicles === 'auto'
  return (
    <Field>
      <FieldLabel className="text-hud-sub text-muted-foreground">{t('task.f.vehicles')}</FieldLabel>
      <div className="flex items-center gap-1.5">
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[auto ? 'auto' : 'fixed']} aria-label={t('task.f.vehicles')}
          onValueChange={(v: unknown[]) => v[0] && taskDraft.setForm({ nVehicles: v[0] === 'auto' ? 'auto' : typeof f.nVehicles === 'number' ? f.nVehicles : 2 })}>
          <ToggleGroupItem value="auto">{t('task.f.auto')}</ToggleGroupItem>
          <ToggleGroupItem value="fixed">{t('task.f.fixed')}</ToggleGroupItem>
        </ToggleGroup>
        {!auto ? <NumField id="task-n" label="" value={f.nVehicles as number} range={{ min: 1, max: 12 }} digits={0} className="w-20"
          onCommit={(v) => (taskDraft.setForm({ nVehicles: v }), yes())} /> : null}
      </div>
    </Field>
  )
}

function ClassLevel({ f }: { f: TaskForm }) {
  const t = useT()
  const g = requiredGsdM(f.targetClass, f.levelReq)
  return (
    <>
      <Field>
        <FieldLabel className="text-hud-sub text-muted-foreground">{t('task.f.class')}</FieldLabel>
        <Select items={TARGET_CLASSES.map((c) => ({ value: c, label: t(`target.class.${c}`) }))} value={f.targetClass}
          onValueChange={(v) => typeof v === 'string' && taskDraft.setForm({ targetClass: v as TargetClassName })}>
          <SelectTrigger size="sm" className="w-full" data-task-class=""><SelectValue /></SelectTrigger>
          <SelectContent>{TARGET_CLASSES.map((c) => <SelectItem key={c} value={c}>{t(`target.class.${c}`)}</SelectItem>)}</SelectContent>
        </Select>
      </Field>
      <Toggle2 id="level" label={t('task.f.level')} value={f.levelReq} items={LEVELS.map((l) => ({ v: l, label: t(`perception.best.${l}`) }))}
        onChange={(levelReq) => taskDraft.setForm({ levelReq })} />
      <p className="col-span-2 text-hud-sub text-muted-foreground" data-task-gsd-req={g}>
        {t('task.f.gsdReq', { cls: t(`target.class.${f.targetClass}`), level: f.levelReq, cm: fmt.num(g * 100, 1) })}
      </p>
    </>
  )
}

/** the parameters of a type (AWR-14 §20.9.1 column 3) */
function Params({ type, f }: { type: TaskType; f: TaskForm }) {
  const t = useT()
  const set = (p: Partial<TaskForm>) => taskDraft.setForm(p)
  const num = (id: keyof TaskForm, label: string, unit: string, min: number, max: number, digits = 0) => (
    <NumField id={`task-${String(id)}`} label={label} value={f[id] as number | null} unit={unit} range={{ min, max }} digits={digits}
      onCommit={(v) => (set({ [id]: v } as Partial<TaskForm>), yes())} />
  )
  switch (type) {
    case 'point_watch':
      return (
        <div className="grid grid-cols-2 gap-2" data-task-params={type}>
          {num('altM', t('task.f.alt'), 'm', 10, 500)}
          <Toggle2 id="altRef" label={t('task.f.altRef')} value={f.altRef} items={[{ v: 'agl', label: 'AGL' }, { v: 'abs', label: t('task.f.abs') }]} onChange={(altRef) => set({ altRef })} />
          <Field>
            <FieldLabel className="text-hud-sub text-muted-foreground">{t('task.f.dwell')}</FieldLabel>
            <div className="flex items-center gap-2">
              <Switch checked={f.dwellS === null} onCheckedChange={(on: boolean) => set({ dwellS: on ? null : 300 })} aria-label={t('task.f.untilCancel')} />
              <span className="text-hud-sub">{t('task.f.untilCancel')}</span>
            </div>
          </Field>
          {f.dwellS !== null ? num('dwellS', t('task.f.dwellS'), 's', 10, 86400) : <span />}
          <Toggle2 id="look" label={t('task.f.look')} value={f.look} items={(['point', 'heading', 'target'] as const).map((v) => ({ v, label: t(`task.look.${v}`) }))}
            onChange={(look) => set({ look })} />
          {num('loiterRadiusM', t('task.f.loiter'), 'm', 30, 2000)}
          <p className="col-span-2 text-hud-sub text-muted-foreground">{t('task.f.loiterHint')}</p>
        </div>
      )
    case 'polyline_patrol':
      return (
        <div className="grid grid-cols-2 gap-2" data-task-params={type}>
          <Toggle2 id="lineMode" label={t('task.f.lineMode')} value={f.lineMode} items={(['once', 'pingpong', 'loop'] as const).map((v) => ({ v, label: t(`target.loop.${v}`) }))}
            onChange={(lineMode) => set({ lineMode })} />
          {num('laps', t('task.f.laps'), '', 1, 100)}
          {num('altM', t('task.f.alt'), 'm', 10, 500)}
          <NumField id="task-speed" label={t('task.f.speed')} value={f.speedMps} unit="m/s" range={{ min: 1, max: 40 }} digits={1}
            onCommit={(v) => (set({ speedMps: v }), yes())} />
          <Toggle2 id="facing" label={t('task.f.facing')} value={f.facing} items={(['forward', 'down', 'side'] as const).map((v) => ({ v, label: t(`task.facing.${v}`) }))}
            onChange={(facing) => set({ facing })} />
        </div>
      )
    case 'area_patrol':
      return (
        <div className="grid grid-cols-2 gap-2" data-task-params={type}>
          <VehiclesField f={f} />
          {num('revisitS', t('task.f.revisit'), 's', 30, 86400)}
          <ClassLevel f={f} />
          <NumField id="task-duration" label={t('task.f.duration')} value={f.durationS} unit="s" range={{ min: 60, max: 604800 }} digits={0}
            onCommit={(v) => (set({ durationS: v }), yes())} />
        </div>
      )
    case 'area_scan_gsd':
      return (
        <div className="grid grid-cols-2 gap-2" data-task-params={type}>
          {num('gsdCm', t('task.f.gsd'), 'cm/px', 0.5, 30, 1)}
          {num('sidelap', t('task.f.sidelap'), '', 0, 0.8, 2)}
          {num('thetaMaxDeg', t('task.f.theta'), '°', 0, 45)}
          <NumField id="task-tmax" label={t('task.f.tMax')} value={f.tMaxS} unit="s" range={{ min: 60, max: 7200 }} digits={0}
            onCommit={(v) => (set({ tMaxS: v }), yes())} />
          {num('hCapM', t('task.f.hCap'), 'm', 20, 500)}
          {num('clearanceM', t('task.f.clearance'), 'm', 0, 200)}
          <VehiclesField f={f} />
          <Field orientation="horizontal" className="items-center">
            <Switch id="task-multi" checked={f.multiSortie} onCheckedChange={(on: boolean) => set({ multiSortie: on })} />
            <FieldLabel htmlFor="task-multi" className="font-normal">{t('task.f.multiSortie')}</FieldLabel>
          </Field>
          <p className="col-span-2 flex items-center gap-1.5 text-hud-sub text-muted-foreground">
            {t('task.f.tMaxHint')}
            <Button size="xs" variant="ghost" className="ml-auto" onClick={() => gsdCalc.open()} data-task-gsd-calc=""><Icon icon="perf.chart" data-icon="inline-start" />{t('gsd.open')}</Button>
          </p>
        </div>
      )
    case 'perimeter_patrol':
      return (
        <div className="grid grid-cols-2 gap-2" data-task-params={type}>
          {num('offsetM', t('task.f.offset'), 'm', -500, 500)}
          <Toggle2 id="direction" label={t('task.f.direction')} value={f.direction} items={(['ccw', 'cw'] as const).map((v) => ({ v, label: t(`task.dir.${v}`) }))}
            onChange={(direction) => set({ direction })} />
          {num('revisitS', t('task.f.revisit'), 's', 30, 86400)}
          <VehiclesField f={f} />
        </div>
      )
    case 'relay_watch':
      return (
        <div className="grid grid-cols-2 gap-2" data-task-params={type}>
          <Toggle2 id="relayLevel" label={t('task.f.level')} value={f.relayLevel ?? 'target'}
            items={[{ v: 'target', label: t('task.f.levelOfTarget') }, ...LEVELS.map((l) => ({ v: l, label: t(`perception.best.${l}`) }))]}
            onChange={(v) => set({ relayLevel: v === 'target' ? null : (v as Level) })} />
          <Field>
            <FieldLabel className="text-hud-sub text-muted-foreground">{t('task.f.duration')}</FieldLabel>
            <div className="flex items-center gap-2">
              <Switch checked={f.durationS === null} onCheckedChange={(on: boolean) => set({ durationS: on ? null : 3600 })} aria-label={t('task.f.continuous')} />
              <span className="text-hud-sub">{t('task.f.continuous')}</span>
            </div>
          </Field>
          {f.durationS !== null ? num('durationS', t('task.f.durationS'), 's', 60, 604800) : null}
          <p className="col-span-2 text-hud-sub text-muted-foreground">{t('task.f.relayHint')}</p>
        </div>
      )
    case 'area_guard':
      return (
        <div className="grid grid-cols-2 gap-2" data-task-params={type}>
          <ClassLevel f={f} />
          {num('revisitS', t('task.f.revisit'), 's', 30, 86400)}
          <p className="col-span-2 text-hud-sub text-muted-foreground">{t('task.f.guardHint')}</p>
        </div>
      )
  }
}

/** priority, detection action and allowed nests (the "more" Collapsible) */
function More({ f }: { f: TaskForm }) {
  const t = useT()
  const nests = useTasking((s) => s.nests)
  return (
    <Collapsible>
      <CollapsibleTrigger render={<Button size="xs" variant="ghost" className="w-full justify-between" data-task-more="" />}>
        {t('task.more')}
        <Icon icon="chev.down" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="grid grid-cols-2 gap-2 pt-1">
          <Toggle2 id="priority" label={t('task.f.priority')} value={f.priority} items={(['low', 'normal', 'high'] as const).map((v) => ({ v, label: t(`task.priority.${v}`) }))}
            onChange={(priority) => taskDraft.setForm({ priority })} />
          <Toggle2 id="onDetect" label={t('task.f.onDetect')} value={f.onDetect} items={(['notify', 'relay', 'ignore'] as const).map((v) => ({ v, label: t(`task.onDetect.${v}`) }))}
            onChange={(onDetect) => taskDraft.setForm({ onDetect })} />
          <Field className="col-span-2">
            <FieldLabel className="text-hud-sub text-muted-foreground">{t('task.f.nests')}</FieldLabel>
            {nests.length === 0 ? <FieldDescription>{t('task.f.nestsNone')}</FieldDescription> : (
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {nests.map((n) => {
                  const id = n.id ?? ''
                  const on = f.allowedNests.includes(id)
                  return (
                    <Field key={id} orientation="horizontal" className="w-auto gap-1.5">
                      <Checkbox id={`task-nest-${id}`} checked={on}
                        onCheckedChange={(c: boolean) => taskDraft.setForm({ allowedNests: c ? [...f.allowedNests, id] : f.allowedNests.filter((x) => x !== id) })} />
                      <FieldLabel htmlFor={`task-nest-${id}`} className="font-normal">{sanitizeText(n.name ?? id, 24)}</FieldLabel>
                    </Field>
                  )
                })}
              </div>
            )}
            <FieldDescription>{t('task.f.nestsHint')}</FieldDescription>
          </Field>
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

export function TaskNewPanel() {
  const t = useT()
  const type = useTaskDraft((s) => s.type)
  const phase = useTaskDraft((s) => s.phase)
  const geom = useTaskDraft((s) => s.geom)
  const form = useTaskDraft((s) => s.form)
  const canEdit = useD2Write()
  const preview = useTasking((s) => s.preview)
  const [starting, setStarting] = React.useState(false)
  // leaving the page (rail page change, unmount) ends the drawing tool; the draft stays for a return to the page
  React.useEffect(() => () => d2Tools.end(), [])
  const issues = type ? precheck(type, geom, form) : []
  const spec = type ? buildSpec(type, geom, form) : null
  const pending = preview?.state === 'pending'
  const done = preview?.state === 'done'
  const doPreview = () => {
    if (!spec) return
    void taskingActions.preview(spec)
  }
  const start = async () => {
    if (!spec) return
    setStarting(true)
    const r = await taskingActions.create(spec)
    setStarting(false)
    if (r.ok) {
      notify(`task:create:${r.data?.tid ?? ''}`, 'info', t('task.created', { tid: r.data?.tid ?? '' }))
      taskDraft.close()
    } else {
      notify('task:create', 'warning', r.code ? reasonText(r.code).short : t('task.createFailed'))
    }
  }
  return (
    <div className="flex min-h-0 flex-col gap-3 overflow-y-auto pr-1" data-task-new={phase}>
      <div className="flex items-center gap-1.5">
        <Button size="xs" variant="ghost" onClick={() => taskDraft.close()} data-task-back=""><Icon icon="chev.left" data-icon="inline-start" />{t('task.back')}</Button>
        <span className="font-medium">{t('task.new.title')}</span>
      </div>
      {!canEdit ? <p className="text-hud-sub text-muted-foreground" data-task-readonly="">{t('task.readOnly')}</p> : null}
      <ToggleGroup spacing={1} size="sm" variant="outline" value={type ? [type] : []} aria-label={t('task.f.type')} className="flex-wrap justify-start"
        data-task-types="" onValueChange={(v: unknown[]) => typeof v[0] === 'string' && taskDraft.chooseType(v[0] as TaskType)}>
        {TASK_TYPE_ORDER.map((k) => (
          <Tooltip key={k}>
            <TooltipTrigger render={<ToggleGroupItem value={k} aria-label={t(`task.type.${k}`)} data-task-type={k} disabled={!canEdit} />}>
              <Icon icon={TASK_ICON[k]} data-icon="inline-start" />
              {t(`task.typeShort.${k}`)}
            </TooltipTrigger>
            <TooltipContent className="max-w-64">{t(`task.typeHint.${k}`)}</TooltipContent>
          </Tooltip>
        ))}
      </ToggleGroup>
      {!type ? <p className="text-hud-sub text-muted-foreground">{t('task.chooseType')}</p> : null}
      {type ? (
        <>
          <p className="flex items-center gap-1.5 text-hud-sub">
            <Badge variant="outline" className="gap-1"><Icon icon={TASK_ICON[type]} />{t(`task.type.${type}`)}</Badge>
            <span className="text-muted-foreground">{t(`task.fit.${type}`)}</span>
          </p>
          {phase === 'drawing' && drawKindOf(type) !== 'target' ? <DrawHint type={type} /> : <GeomLine type={type} />}
          {phase === 'params' || drawKindOf(type) === 'target' ? (
            <>
              <Field>
                <FieldLabel htmlFor="task-label" className="text-hud-sub text-muted-foreground">{t('task.f.label')}</FieldLabel>
                <InputGroup>
                  <InputGroupInput id="task-label" value={form.label} maxLength={32} placeholder={t(`task.type.${type}`)}
                    onChange={(e) => taskDraft.setForm({ label: e.target.value })} />
                </InputGroup>
              </Field>
              <Params type={type} f={form} />
              <More f={form} />
              {issues.length ? (
                <Alert className="border-dashed" data-task-issues={issues.length}>
                  <Icon icon="alert.warning" />
                  <AlertDescription className="flex flex-col gap-0.5">
                    {issues.map((i) => (
                      <span key={i.key} data-task-issue={i.key}>
                        {t(i.key, { value: i.value !== undefined ? fmt.num(i.key === 'task.issue.area' ? i.value / 1e6 : i.value, i.key === 'task.issue.area' ? 2 : 0) : '' })}
                        {i.code ? ` (${i.code})` : ''}
                      </span>
                    ))}
                  </AlertDescription>
                </Alert>
              ) : null}
              <div className="flex items-center gap-1.5">
                <Button size="sm" variant="outline" disabled={!canEdit || blocking(issues) || pending} onClick={doPreview} data-task-preview="">
                  {pending ? <Spinner data-icon="inline-start" /> : <Icon icon="layer.visible" data-icon="inline-start" />}
                  {t('task.preview')}
                </Button>
                <Button size="sm" className={cn('ml-auto')} disabled={!canEdit || !done || starting || blocking(issues)} onClick={() => void start()} data-task-start="">
                  {starting ? <Spinner data-icon="inline-start" /> : <Icon icon="mission.start" data-icon="inline-start" />}
                  {t('task.start')}
                </Button>
              </div>
              {preview ? <TaskPreviewCard type={type} /> : <p className="text-hud-sub text-muted-foreground">{t('task.previewFirst')}</p>}
            </>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
