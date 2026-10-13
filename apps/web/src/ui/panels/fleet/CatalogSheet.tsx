// Model catalogue Sheet and clone editor (R-D2-13, R-D2-15, R-D2-27; AWR-04 §5; AWR-20; M15-FR-211; UX-FR-115, UX-FR-116;
// AWR-14 §20.6.2, §20.6.3): category filter and search, one card per model (category, mass, endurance, cruise speed,
// link range, default sensors; reference models carry "simulation reference values, not any real product", P600 says
// its values come from the supplied hardware sheet; the session's clones come last with "this session"), the parameter
// table of a model (lieflat table.log: group, parameter, value with unit, confidence A-D, simulation use) with the instant
// GSD and recognition-distance line of its default camera (M19 calculator: GSD at 100 m, the largest range per level for a
// person, the station window), and, for the sandbox operator, "clone and edit": whitelisted fields with units and
// front-end ranges (the battery cell count is locked, CL-4), derived values recomputed as the form changes, only the
// changed fields are sent (SB-25 `patch` in dotted paths), and the 522 violations land on the fields (M21 mapViolations)
// or in the form's alert. Data: M21's stores/catalog.ts (public catalogue with ETag, the session's clones). The catalogue
// is public and cacheable, so the shared show browses it read-only (D2-AC-37).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { catalog, isReference, useCatalog, visibleModels, type ModelSummary, type ParamRow, type SensorItem } from '@/stores/catalog'
import { sensorFromCatalog, sensorSummary } from '@/app/shell/engineD2'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/ui/components/ui/accordion'
import { Alert, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Field, FieldError, FieldLabel } from '@/ui/components/ui/field'
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/ui/components/ui/input-group'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Spinner } from '@/ui/components/ui/spinner'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { TextField } from '@/ui/sandbox/fields'
import { useSurface } from '@/app/shell/surface'
import { useSandbox } from '@/ui/sandbox/session'
import { gsdCalc } from '@/ui/views/GsdCalculator'
import { labelOk } from '@/ui/panels/targets/targetModel'
import { baseValues, CATEGORIES, CLONE_FIELDS, changedFields, cloneErrors, derived, LOCKED, type Category, type CloneValues } from './fleetModel'

const fmtMin = (s: number | undefined): string => (s === undefined ? '-' : `${fmt.num(s / 60, 0)} min`)

function ModelCard({ m, session, onDetail, onClone, operator }: { m: ModelSummary; session: boolean; onDetail: () => void; onClone: () => void; operator: boolean }) {
  const t = useT()
  const end = m.category === 'multirotor' ? m.endurance?.hover_s : m.endurance?.cruise_s
  return (
    <Card size="sm" data-model-card={m.model_id}>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5">
          <Icon icon={m.category === 'multirotor' ? 'drone.quad' : 'fw.plane'} />
          <span className="truncate">{m.name_zh ? sanitizeText(m.name_zh, 32) : m.model_id}</span>
          {session ? <Badge variant="secondary">{t('catalog.session')}</Badge> : null}
        </CardTitle>
        <CardDescription className="flex flex-col gap-0.5">
          {isReference(m) ? <Badge variant="outline" className="self-start" data-model-reference="">{t('catalog.reference')}</Badge> : null}
          {m.model_id.startsWith('p600') ? <span>{t('catalog.p600Source')}</span> : null}
          <span>{`${t(`catalog.cat.${m.category}`)} · ${fmt.num(m.mass_kg ?? Number.NaN, 2)} kg · ${t(m.category === 'multirotor' ? 'catalog.hover' : 'catalog.cruise')} ${fmtMin(end)}`}</span>
          <span>{`${t('catalog.speed')} ${fmt.num(m.speed?.cruise_mps ?? Number.NaN, 0)} m/s · ${t('catalog.link')} ${fmt.num((m.link_range_m ?? Number.NaN) / 1000, 1)} km`}</span>
          <span className="truncate font-mono">{(m.default_sensors ?? []).join(' · ') || '-'}</span>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex gap-1.5">
        <Button size="xs" variant="outline" onClick={onDetail} data-model-detail={m.model_id}>{t('catalog.detail')}</Button>
        {operator ? <Button size="xs" variant="ghost" onClick={onClone} data-model-clone={m.model_id}>{t('catalog.clone')}</Button> : null}
      </CardContent>
    </Card>
  )
}

/** a parameter value as text (numbers formatted, strings sanitised, structures as JSON) */
function valueText(v: unknown): string {
  if (typeof v === 'number') return fmt.num(v, 3)
  if (typeof v === 'string') return sanitizeText(v, 32)
  if (typeof v === 'boolean') return String(v)
  return v === null || v === undefined ? '-' : sanitizeText(JSON.stringify(v), 48)
}

/** the first imaging sensor of a model's default mounts, as a catalogue sensor item */
export function cameraOf(m: Pick<ModelSummary, 'default_sensors'>, sensors: readonly SensorItem[]): SensorItem | null {
  for (const name of m.default_sensors ?? []) {
    const it = sensors.find((s) => s.sensor_type === name && (s.kind === 'eo' || s.kind === 'nir' || s.kind === 'lwir'))
    if (it) return it
  }
  return null
}

/** the instant GSD and recognition-distance line of a model's default camera (M19 calculator, person, MOR 10 km) */
function GsdHint({ m, holdSigmaM }: { m: Pick<ModelSummary, 'default_sensors'>; holdSigmaM?: number }) {
  const t = useT()
  const sensors = useCatalog((s) => s.sensors)
  const item = cameraOf(m, sensors)
  const sum = React.useMemo(() => {
    const sn = item ? sensorFromCatalog(item) : null
    return sn ? { sn, s: sensorSummary(sn, { cls: 'person', holdSigmaM }) } : null
  }, [item, holdSigmaM])
  if (!item || !sum) return null
  const w = sum.s.window.day ?? sum.s.window.night ?? null
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-hud-sub text-muted-foreground" data-catalog-gsd={item.sensor_type}>
      <span>{t('catalog.gsdHint', { sensor: sanitizeText(item.name_zh || item.sensor_type, 24), f: fmt.num(sum.s.fMm, 1), gsd: fmt.num(sum.s.gsdAt100M * 100, 2) })}</span>
      <span>{t('catalog.rangeHint', { d: fmt.num(sum.s.maxRangeM.D, 0), r: fmt.num(sum.s.maxRangeM.R, 0), i: fmt.num(sum.s.maxRangeM.I, 0) })}</span>
      <span>{w ? t('catalog.windowHint', { lo: fmt.num(w.dLoM, 0), hi: fmt.num(w.dHiM, 0) }) : t('catalog.noWindow')}</span>
      <Button size="xs" variant="ghost" className="h-5 px-1" onClick={() => gsdCalc.open(`cat:${item.sensor_type}`)}><Icon icon="perf.chart" data-icon="inline-start" />{t('gsd.open')}</Button>
    </p>
  )
}

function ModelParams({ id }: { id: string }) {
  const t = useT()
  const d = useCatalog((s) => s.details.get(id) ?? null)
  const [failed, setFailed] = React.useState(false)
  React.useEffect(() => {
    let live = true
    void catalog.detail(id).then((r) => {
      if (live && !r) setFailed(true)
    })
    return () => {
      live = false
    }
  }, [id])
  if (!d) return failed ? <p className="text-muted-foreground">{t('catalog.unavailable')}</p> : <Spinner />
  const rows: ParamRow[] = d.params_table ?? []
  const columns: LfColumn<ParamRow>[] = [
    { key: 'group', label: t('catalog.col.group'), width: '5rem', format: (r) => sanitizeText(r.group ?? '', 24) },
    { key: 'label', label: t('catalog.col.param'), format: (r) => sanitizeText(r.label_zh ?? r.key, 40) },
    { key: 'value', label: t('catalog.col.value'), align: 'right', format: (r) => `${valueText(r.value)}${r.unit ? ` ${r.unit}` : ''}` },
    { key: 'conf', label: t('catalog.col.confidence'), width: '4rem', format: (r) => <Badge variant="outline">{r.confidence === 'label' ? t('catalog.label') : r.confidence}</Badge> },
    { key: 'use', label: t('catalog.col.use'), format: (r) => (r.confidence === 'label' ? t('catalog.labelUse') : sanitizeText(r.use_zh ?? '', 40)) },
  ]
  return (
    <div className="flex flex-col gap-2">
      <GsdHint m={d} />
      <LfTable columns={columns} rows={rows} rowKey={(r) => r.key} ariaLabel={t('catalog.params')} height={320} figureId={`catalog-${id}`} />
    </div>
  )
}

function CloneForm({ base, onDone }: { base: ModelSummary; onDone: () => void }) {
  const t = useT()
  const sid = useSandbox((s) => s.sid)
  const saving = useCatalog((s) => s.saving)
  const fieldErrors = useCatalog((s) => s.fieldErrors)
  const formErrors = useCatalog((s) => s.formErrors)
  const detail = useCatalog((s) => s.details.get(base.model_id) ?? null)
  // the form opens after the detail is loaded (CatalogSheet), so the initial values include its parameter rows
  const initial = React.useMemo(() => baseValues(base, detail), [base, detail])
  const [label, setLabel] = React.useState(`${base.name_zh ?? base.model_id}`)
  const [values, setValues] = React.useState<CloneValues>(initial)
  React.useEffect(() => () => catalog.clearErrors(), [])
  const cat: Category = base.category
  const errs = cloneErrors(values, cat)
  const d = derived(values)
  const fields = CLONE_FIELDS.filter((f) => f.cats.includes(cat))
  const save = async () => {
    if (!sid) return
    const r = await catalog.createClone(sid, { from_model_id: base.model_id, label: label.trim(), patch: changedFields(initial, values) }, fields.map((f) => f.key))
    if (r) onDone()
  }
  return (
    <div className="flex flex-col gap-3" data-clone-form={base.model_id}>
      <TextField id="clone-label" label={t('catalog.f.label')} value={label} validate={labelOk} onCommit={async (v) => {
        setLabel(v)
        return true
      }} />
      {formErrors.length ? <Alert className="border-dashed" data-clone-errors=""><Icon icon="alert.warning" /><AlertDescription>{formErrors.join(' · ')}</AlertDescription></Alert> : null}
      <div className="grid grid-cols-2 gap-2">
        {fields.map((f) => {
          const bad = errs[f.key] ?? null
          const srv = fieldErrors[f.key] ?? null
          const locked = LOCKED.has(f.key)
          return (
            <Field key={f.key} data-clone-field={f.key} data-invalid={bad || srv ? '' : undefined}>
              <FieldLabel htmlFor={`clone-${f.key}`} className="text-hud-sub text-muted-foreground">{t(`catalog.f.${f.key}`)}</FieldLabel>
              <InputGroup>
                <InputGroupInput id={`clone-${f.key}`} inputMode="decimal" aria-invalid={!!(bad || srv)} readOnly={locked} placeholder={t('catalog.unknown')}
                  value={Number.isFinite(values[f.key]) ? String(values[f.key]) : ''}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value === '' ? Number.NaN : Number(e.target.value) })} />
                {f.unit ? <InputGroupAddon align="inline-end">{f.unit}</InputGroupAddon> : null}
              </InputGroup>
              {bad ? <FieldError>{t(`catalog.rule.${bad}`, { min: f.range.min, max: f.range.max })}</FieldError> : srv ? <FieldError>{srv}</FieldError> : locked ? <span className="text-hud-cap text-muted-foreground">{t('catalog.locked')}</span> : null}
            </Field>
          )
        })}
      </div>
      <p className="text-hud-sub text-muted-foreground" data-clone-derived="">{t('catalog.derived', { wh: fmt.num(d.wh, 0), use: fmt.num(d.useWh, 0) })}</p>
      <GsdHint m={base} />
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onDone}>{t('common.cancel')}</Button>
        <Button disabled={saving || Object.keys(errs).length > 0 || !labelOk(label) || !sid} onClick={() => void save()} data-clone-save="">
          {saving ? <Spinner data-icon="inline-start" /> : null}{t('catalog.save')}
        </Button>
      </div>
    </div>
  )
}

export function CatalogSheet({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const t = useT()
  const operator = useSurface((s) => s.surface === 'sandbox')
  const sid = useSandbox((s) => s.sid)
  const state = useCatalog((s) => s.state)
  const filter = useCatalog((s) => s.filter)
  const query = useCatalog((s) => s.query)
  const models = useCatalog((s) => s.models)
  const sessionModels = useCatalog((s) => s.sessionModels)
  const [detail, setDetail] = React.useState<string | null>(null)
  const [clone, setClone] = React.useState<ModelSummary | null>(null)
  React.useEffect(() => {
    if (!open) return
    // models first: a failing sensor list must not hold the cards back (the second call is a 304 for the models)
    void catalog.load().then(() => catalog.load({ sensors: true }))
    if (operator && sid) void catalog.loadSession(sid)
  }, [open, operator, sid])
  const list = visibleModels({ models, sessionModels: operator ? sessionModels : [], filter, query })
  return (
    <Sheet open={open} onOpenChange={(o) => {
      onOpenChange(o)
      if (!o) setClone(null)
    }}>
      <SheetContent side="right" className="w-140 max-w-full sm:max-w-140" data-catalog-sheet="">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2"><Icon icon="drone.quad" />{clone ? t('catalog.cloneTitle', { model: clone.name_zh ?? clone.model_id }) : t('catalog.title')}</SheetTitle>
          <SheetDescription>{t('catalog.desc')}</SheetDescription>
        </SheetHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 pb-4">
          {clone ? <CloneForm base={clone} onDone={() => setClone(null)} /> : (
            <>
              <div className="flex items-center gap-2">
                <ToggleGroup spacing={0} size="sm" variant="outline" value={[filter]} aria-label={t('catalog.category')}
                  onValueChange={(v: unknown[]) => typeof v[0] === 'string' && catalog.setFilter(v[0] as Category | 'all')}>
                  <ToggleGroupItem value="all">{t('common.all')}</ToggleGroupItem>
                  {CATEGORIES.map((c) => <ToggleGroupItem key={c} value={c}>{t(`catalog.cat.${c}`)}</ToggleGroupItem>)}
                </ToggleGroup>
                <InputGroup className="ml-auto w-40">
                  <InputGroupInput placeholder={t('catalog.search')} value={query} onChange={(e) => catalog.setQuery(e.target.value)} aria-label={t('catalog.search')} />
                </InputGroup>
              </div>
              {state === 'loading' && models.length === 0 ? <Spinner /> : state === 'error' && models.length === 0 ? <p className="text-muted-foreground">{t('catalog.unavailable')}</p> : null}
              <div className="grid grid-cols-2 gap-2" data-catalog-models={list.length}>
                {list.map((m) => (
                  <ModelCard key={m.model_id} m={m} session={m.session_clone === true} operator={operator}
                    onDetail={() => setDetail(detail === m.model_id ? null : m.model_id)} onClone={() => void catalog.detail(m.model_id).finally(() => setClone(m))} />
                ))}
              </div>
              {detail ? (
                <Accordion defaultValue={['detail']}>
                  <AccordionItem value="detail">
                    <AccordionTrigger>{t('catalog.params')} · {detail}</AccordionTrigger>
                    <AccordionContent><ModelParams id={detail} /></AccordionContent>
                  </AccordionItem>
                </Accordion>
              ) : null}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
