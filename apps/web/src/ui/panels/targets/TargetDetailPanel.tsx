// Target detail page (R-D2-08 to R-D2-11, R-D2-18; M15-FR-212; UX-FR-120 to UX-FR-124; AWR-14 §20.7.2 to §20.7.5): the
// live state from `target/{id}/detail` (2 Hz: awareness, discovered count and the last discovery, dwell progress t /
// t_dwell, the best capture), the common properties (label, enabled, size, motion with "draw path" and "draw wander
// region", contrast, temperature difference, sound source, criterion mode, required level, pixel requirement), the
// sensitivity volumes (inline table, at most 4; display of the configured or the effective volume for a reference model;
// "adopt the suggested radius" with the before and after list) and the advanced properties Sheet (nine groups; the radio
// group is stored only in D2; the sound group draws one period of the source as a lieflat line, P1). Edits are committed
// field by field (M18 targetsActions.update: JSON Merge Patch over the WS call `target/update`); a refusal rolls the
// field back and toasts the reason (540 to 544). The shared show and the viewer see the same page read-only. Data: M18's
// stores/targets.ts (the selected target's `target/{id}/detail` at 2 Hz, the configuration kept by the target model,
// the body view of the layer: configured, effective for a model, planning); the suggested radius is the sandbox REST.
import type { Sensitivity, Sound, Target, TargetSuggestedRadius } from '@awr/contracts/types'
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { ApiError } from '@/net/api'
import { targetsActions, useTargets } from '@/stores/targets'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { soundPeriodSamples, targetsModel, type BodyView } from '@/app/shell/engineD2'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/ui/components/ui/accordion'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader,
  AlertDialogTitle,
} from '@/ui/components/ui/alert-dialog'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Progress } from '@/ui/components/ui/progress'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Switch } from '@/ui/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tabs, TabsContent, TabsList, TabsPanels, TabsTrigger } from '@/ui/components/ui/tabs'
import { Icon } from '@/ui/icons/Icon'
import { LfLine } from '@/ui/lf/LfLine'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { PanelEmpty } from '@/ui/brand'
import { confirmThen } from '@/ui/actions/ConfirmHost'
import { useSurface } from '@/app/shell/surface'
import { confirmDiscovery } from '@/ui/notify/discoveries'
import { NumField, TextField } from '@/ui/sandbox/fields'
import { sbSession } from '@/ui/sandbox/domain'
import { entities, useEntities } from '@/ui/sandbox/entities'
import { d2Tools, useD2Tool } from '@/ui/tools/d2Tools'
import { CLASS_ICON, targetCallText } from './TargetsPanel'
import { labelOk, MAX_VOLUMES, newVolume, RANGES, sensitivityErrors } from './targetModel'
import { QuantCard } from './QuantCard'

function usePatch(id: string | null) {
  return React.useCallback(async (p: Partial<Target>): Promise<boolean> => {
    if (!id) return false
    const o = await targetsActions.update(id, p as Parameters<typeof targetsActions.update>[1])
    if (!o.ok) notify(`targets:patch:${id}`, 'warning', targetCallText(o))
    return o.ok
  }, [id])
}

/** the selected target's detail (awr.target.detail.v1, 2 Hz while selected) */
function useDetail(id: string | null) {
  return useTargets((s) => (s.selected === id ? s.detail : null))
}

function LiveState({ id }: { id: string }) {
  const t = useT()
  const d = useDetail(id)
  if (!d) return <p className="text-hud-sub text-muted-foreground">{t('common.loading')}</p>
  const dwell = d.dwell
  const last = d.last_discovered
  return (
    <div className="flex flex-col gap-1.5 text-hud-sub" data-target-live={d.awareness}>
      <span className="flex items-center gap-2">
        <span className="text-muted-foreground">{t('target.awareness')}</span>
        <Badge variant="outline">{t(`awareness.${d.awareness}`)}</Badge>
        <span className="ml-auto text-muted-foreground">{t('target.discoveredN', { n: d.discovered_count })}</span>
      </span>
      {dwell ? (
        <span className="flex flex-col gap-1" data-target-dwell="">
          <span className="text-muted-foreground">{t('target.dwell', { uav: sanitizeText(dwell.uav, 32), t: fmt.num(dwell.t_s, 1), of: fmt.num(dwell.t_dwell_s, 1) })}</span>
          <Progress value={Math.min(100, Math.round((dwell.t_s / Math.max(0.1, dwell.t_dwell_s)) * 100))} aria-label={t('target.dwellLabel')} />
        </span>
      ) : null}
      {last ? (
        <span className="flex items-center gap-2" data-target-last="">
          <Icon icon="alert.warning" />
          {t('target.lastDiscovered', { uav: sanitizeText(last.uav, 32), kind: t(`sens.kind.${last.kind}`), range: fmt.num(last.range_m ?? Number.NaN, 0) })}
          <Button size="xs" variant="ghost" className="ml-auto" onClick={() => confirmDiscovery(id)}>{t('common.confirm')}</Button>
        </span>
      ) : null}
      {d.capture ? (
        <span className="flex items-center gap-2 text-muted-foreground" data-target-capture={d.capture.best_level}>
          {t(`perception.best.${d.capture.best_level}`)}
          {d.capture.captured ? <Badge variant="secondary"><Icon icon="effect.verified" />{t('target.captured')}</Badge> : null}
          {d.capture.uav ? <span className="font-mono">{sanitizeText(d.capture.uav, 32)}</span> : null}
          {d.capture.n_eff !== null && d.capture.n_eff !== undefined ? <span className="font-mono">{`${fmt.num(d.capture.n_eff, 1)} / ${fmt.num(d.capture.n_req ?? Number.NaN, 0)} px`}</span> : null}
        </span>
      ) : null}
    </div>
  )
}

/** sensitivity volumes: inline table, configured or effective display, suggested radius */
function SensitivityEditor({ id, target, operator, sandbox }: { id: string; target: Target; operator: boolean; sandbox: boolean }) {
  const t = useT()
  const detail = useDetail(id)
  const patch = usePatch(id)
  const bodyView = useTargets((s) => s.bodyView)
  const modelId = useTargets((s) => s.modelId)
  const [suggest, setSuggest] = React.useState<TargetSuggestedRadius | null>(null)
  const vols = target.sensitivity ?? []
  const errors = sensitivityErrors(vols)
  const models = React.useMemo(() => {
    const keys = new Set<string>()
    for (const v of detail?.sensitivity ?? []) for (const k of Object.keys(v.radius_eff_m ?? {})) keys.add(k)
    return [...keys]
  }, [detail])
  const model = modelId ?? models[0] ?? null
  const save = (next: Sensitivity[]) => patch({ sensitivity: next })
  const update = (i: number, p: Partial<Sensitivity>) => save(vols.map((v, k) => (k === i ? { ...v, ...p } : v)))
  const askSuggest = async () => {
    if (!model) return
    try {
      setSuggest(await sbSession.suggestedRadius(id, model))
    } catch (e) {
      notify('targets:suggest', 'warning', e instanceof ApiError && e.reason ? reasonText(e.reason).short : String(e))
    }
  }
  const adopt = () => {
    if (!suggest) return
    const next = vols.map((v, i) => {
      const s = suggest.volumes.find((x) => x.idx === i)
      return s?.radius_suggested_m ? { ...v, radius_max_m: Math.round(s.radius_suggested_m) } : v
    })
    setSuggest(null)
    void save(next)
  }
  type Row = { i: number; v: Sensitivity }
  const rows: Row[] = vols.map((v, i) => ({ i, v }))
  const eff = (i: number): number | null => (model ? detail?.sensitivity?.find((x) => x.idx === i)?.radius_eff_m?.[model] ?? null : null)
  const columns: LfColumn<Row>[] = [
    { key: 'kind', label: t('sens.col.kind'), width: '4.5rem', format: (r) => t(`sens.kind.${r.v.kind}`) },
    { key: 'shape', label: t('sens.col.shape'), width: '4.5rem', format: (r) => t(`sens.shape.${r.v.shape}`) },
    {
      key: 'radius', label: t('sens.col.radius'), unit: 'm', align: 'right', width: '6rem',
      format: (r) => operator ? (
        <NumField id={`sens-r-${r.i}`} label="" value={r.v.radius_max_m} range={RANGES.radius} digits={0} serverError={errors[r.i]?.includes('radius_max_m') ? t('form.invalid') : null}
          onCommit={(x) => update(r.i, { radius_max_m: x })} />
      ) : fmt.num(r.v.radius_max_m, 0),
    },
    { key: 'eff', label: t('sens.col.eff'), unit: 'm', align: 'right', width: '4.5rem', format: (r) => (eff(r.i) === null ? '—' : fmt.num(eff(r.i) as number, 0)) },
    { key: 'z', label: t('sens.col.height'), unit: 'm', align: 'right', width: '5rem', format: (r) => `${fmt.num(r.v.z_min_m ?? 0, 0)}–${fmt.num(r.v.z_max_m ?? r.v.radius_max_m, 0)}` },
    { key: 'angle', label: t('sens.col.angle'), unit: '°', align: 'right', width: '3.5rem', format: (r) => (r.v.shape === 'sector' ? fmt.num(r.v.half_angle_deg ?? 60, 0) : '—') },
    {
      key: 'on', label: t('sens.col.enabled'), width: '3.5rem',
      format: (r) => <Switch checked={r.v.enabled !== false} disabled={!operator} aria-label={t('sens.col.enabled')} onCheckedChange={(on: boolean) => void update(r.i, { enabled: on })} />,
    },
    ...(operator ? [{
      key: 'rm', label: '', width: '2rem',
      format: (r: Row) => <Button size="icon-xs" variant="ghost" aria-label={t('sens.remove')} onClick={() => void save(vols.filter((_, k) => k !== r.i))}><Icon icon="close" /></Button>,
    }] : []),
  ]
  return (
    <div className="flex flex-col gap-2" data-sensitivity="">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('sens.title')}</span>
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[bodyView]} aria-label={t('sens.display')} data-sens-display={bodyView}
          onValueChange={(v: unknown[]) => (v[0] === 'configured' || v[0] === 'effective' || v[0] === 'planning') && targetsActions.setBodyView(v[0] as BodyView, v[0] === 'effective' ? model : undefined)}>
          <ToggleGroupItem value="configured">{t('sens.display.config')}</ToggleGroupItem>
          <ToggleGroupItem value="effective">{t('sens.display.effective')}</ToggleGroupItem>
          <ToggleGroupItem value="planning">{t('sens.display.planning')}</ToggleGroupItem>
        </ToggleGroup>
        {models.length ? (
          <Select items={models.map((m) => ({ value: m, label: m }))} value={model ?? ''} onValueChange={(v) => typeof v === 'string' && targetsActions.setBodyView(bodyView, v)}>
            <SelectTrigger size="sm" aria-label={t('sens.model')}><SelectValue /></SelectTrigger>
            <SelectContent>{models.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}</SelectContent>
          </Select>
        ) : null}
      </div>
      {target.criterion_mode !== 'physical' && bodyView === 'effective' ? <p className="text-hud-sub text-muted-foreground">{t('sens.geometricHint')}</p> : null}
      {bodyView === 'planning' ? <p className="text-hud-sub text-muted-foreground">{t('sens.planningHint')}</p> : null}
      <LfTable columns={columns} rows={rows} rowKey={(r) => String(r.i)} ariaLabel={t('sens.title')} height={200} rowHeight={40} figureId="sensitivity" className="min-w-0" />
      {operator ? (
        <div className="flex flex-wrap gap-1.5">
          <Button size="xs" variant="outline" disabled={vols.length >= MAX_VOLUMES} onClick={() => void save([...vols, newVolume('visual')])} data-sens-add="visual">
            <Icon icon="sens.visual" data-icon="inline-start" />{t('sens.addVisual')}
          </Button>
          <Button size="xs" variant="outline" disabled={vols.length >= MAX_VOLUMES} onClick={() => void save([...vols, newVolume('acoustic')])} data-sens-add="acoustic">
            <Icon icon="sens.acoustic" data-icon="inline-start" />{t('sens.addAcoustic')}
          </Button>
          {sandbox ? <Button size="xs" variant="ghost" disabled={!model} onClick={() => void askSuggest()} data-sens-suggest="">{t('sens.suggest')}</Button> : null}
        </div>
      ) : null}
      <p className="text-hud-cap text-muted-foreground">{t('sens.keepoutNote')}</p>
      <AlertDialog open={suggest !== null} onOpenChange={(o) => (o ? null : setSuggest(null))}>
        <AlertDialogContent data-sens-suggest-dialog="">
          <AlertDialogHeader>
            <AlertDialogTitle>{t('sens.suggestTitle', { model: model ?? '' })}</AlertDialogTitle>
            <AlertDialogDescription>{t('sens.suggestBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="flex flex-col gap-1 text-hud-sub">
            {suggest?.volumes.map((v) => (
              <li key={v.idx} className="flex justify-between font-mono">
                <span className="font-sans">{t(`sens.kind.${v.kind}`)}</span>
                <span>{`${fmt.num(v.radius_cfg_m, 0)} m → ${v.radius_suggested_m ? fmt.num(v.radius_suggested_m, 0) : '—'} m`}</span>
              </li>
            ))}
          </ul>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={adopt} data-sens-adopt="">{t('sens.adopt')}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/** one period of the sound source (lieflat static line, P1 UX-FR-124; M18 soundPeriodSamples, display carrier) */
export function SoundWave({ sound }: { sound: Sound }) {
  const t = useT()
  const data = React.useMemo(() => {
    const n = 240
    const out = new Float32Array(n)
    soundPeriodSamples(sound, n, out)
    return Array.from(out, (v, i) => ({ t: (i / n) * sound.period_s, v }))
  }, [sound])
  return <LfLine mode="static" data={data} peaks={0} hero="none" width={300} height={72} ariaLabel={t('sound.wave', { s: fmt.num(sound.period_s, 1) })} format={(v) => fmt.num(v, 2)} />
}

/** the advanced properties (nine groups, AWR-14 §20.7.3) */
function AdvancedSheet({ open, onOpenChange, target, patch, readOnly }: { open: boolean; onOpenChange: (o: boolean) => void; target: Target; patch: (p: Partial<Target>) => Promise<boolean>; readOnly: boolean }) {
  const t = useT()
  const m = target.motion ?? { mode: 'static' as const }
  const th = target.thermal ?? { mode: 'relative' as const }
  const sound = target.sound ?? null
  const num = (id: string, label: string, value: number | null | undefined, range: { min: number; max: number }, set: (v: number) => Partial<Target>, unit?: string, digits = 2) => (
    <NumField id={id} label={label} value={value} range={range} unit={unit} digits={digits} readOnly={readOnly} onCommit={(v) => patch(set(v))} />
  )
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-140 max-w-full sm:max-w-140" data-target-advanced="">
        <SheetHeader>
          <SheetTitle>{t('target.advanced.title', { label: sanitizeText(target.label ?? target.id ?? '', 32) })}</SheetTitle>
          <SheetDescription>{t('target.advanced.desc')}</SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
          <Accordion defaultValue={['geometry']}>
            <AccordionItem value="geometry">
              <AccordionTrigger>{t('target.group.geometry')}</AccordionTrigger>
              <AccordionContent className="grid grid-cols-2 gap-2">
                {num('crit', t('target.f.criticalDim'), target.critical_dim_m ?? null, RANGES.size, (v) => ({ critical_dim_m: v }), 'm')}
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="motion">
              <AccordionTrigger>{t('target.group.motion')}</AccordionTrigger>
              <AccordionContent className="grid grid-cols-2 gap-2">
                {num('vmax', t('target.f.speedMax'), m.speed_max_mps, RANGES.speed, (v) => ({ motion: { ...m, speed_max_mps: v } }), 'm/s')}
                {num('acc', t('target.f.accel'), m.accel_mps2, { min: 0, max: 20 }, (v) => ({ motion: { ...m, accel_mps2: v } }), 'm/s²')}
                {num('turn', t('target.f.turnRate'), m.turn_rate_max_rad_s, { min: 0, max: 10 }, (v) => ({ motion: { ...m, turn_rate_max_rad_s: v } }), 'rad/s')}
                {num('sigma', t('target.f.speedSigma'), m.speed_sigma_mps, { min: 0, max: 10 }, (v) => ({ motion: { ...m, speed_sigma_mps: v } }), 'm/s')}
                {num('tau', t('target.f.turnTau'), m.turn_tau_s, { min: 0.1, max: 600 }, (v) => ({ motion: { ...m, turn_tau_s: v } }), 's')}
                {num('pause', t('target.f.pauseProb'), m.pause_prob, RANGES.unit, (v) => ({ motion: { ...m, pause_prob: v } }))}
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="appearance">
              <AccordionTrigger>{t('target.group.appearance')}</AccordionTrigger>
              <AccordionContent className="grid grid-cols-2 gap-2">
                {num('albedo', t('target.f.albedo'), target.albedo_vis, RANGES.unit, (v) => ({ albedo_vis: v }))}
                {num('camo', t('target.f.camouflage'), target.camouflage, RANGES.unit, (v) => ({ camouflage: v }))}
                {num('occl', t('target.f.occlusion'), target.occlusion, RANGES.unit, (v) => ({ occlusion: v }))}
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="reflect">
              <AccordionTrigger>{t('target.group.reflectance')}</AccordionTrigger>
              <AccordionContent className="grid grid-cols-2 gap-2">
                {num('r850', t('target.f.reflect850'), target.reflect_850, RANGES.unit, (v) => ({ reflect_850: v }))}
                {num('r905', t('target.f.reflect905'), target.reflect_905, RANGES.unit, (v) => ({ reflect_905: v }))}
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="thermal">
              <AccordionTrigger>{t('target.group.thermal')}</AccordionTrigger>
              <AccordionContent className="grid grid-cols-2 gap-2">
                {num('eps', t('target.f.emissivity'), th.emissivity, RANGES.emissivity, (v) => ({ thermal: { ...th, emissivity: v } }))}
                {num('hot', t('target.f.hotFraction'), th.hot_fraction, RANGES.unit, (v) => ({ thermal: { ...th, hot_fraction: v } }))}
                {num('tsurf', t('target.f.tSurface'), th.t_surface_c, { min: -50, max: 300 }, (v) => ({ thermal: { ...th, mode: 'absolute', t_surface_c: v } }), '°C', 1)}
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="radar">
              <AccordionTrigger>{t('target.group.radar')}</AccordionTrigger>
              <AccordionContent className="grid grid-cols-2 gap-2">
                {num('rcs', t('target.f.rcs'), target.rcs_m2, RANGES.rcs, (v) => ({ rcs_m2: v }), 'm²', 3)}
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="sound">
              <AccordionTrigger>{t('target.group.sound')}</AccordionTrigger>
              <AccordionContent className="flex flex-col gap-2">
                {sound ? (
                  <>
                    <div className="grid grid-cols-2 gap-2">
                      {num('f0', t('target.f.f0'), sound.f0_hz, RANGES.f0, (v) => ({ sound: { ...sound, f0_hz: v } }), 'Hz', 0)}
                      {num('period', t('target.f.period'), sound.period_s, RANGES.period, (v) => ({ sound: { ...sound, period_s: v } }), 's', 1)}
                      {num('duty', t('target.f.duty'), sound.duty, RANGES.unit, (v) => ({ sound: { ...sound, duty: v } }))}
                      {num('phase', t('target.f.phase'), sound.phase_s ?? 0, { min: 0, max: 600 }, (v) => ({ sound: { ...sound, phase_s: v } }), 's', 1)}
                    </div>
                    <span className="text-hud-sub text-muted-foreground">{t(`sound.waveform.${sound.waveform}`)}</span>
                    <SoundWave sound={sound} />
                  </>
                ) : <span className="text-hud-sub text-muted-foreground">{t('target.noSound')}</span>}
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="rf">
              <AccordionTrigger>{t('target.group.rf')}</AccordionTrigger>
              <AccordionContent>
                <p className="text-hud-sub text-muted-foreground">{t('target.rfLater')}</p>
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="behavior">
              <AccordionTrigger>{t('target.group.behavior')}</AccordionTrigger>
              <AccordionContent className="grid grid-cols-2 gap-2">
                {num('alert', t('target.f.alertness'), target.alertness, RANGES.unit, (v) => ({ alertness: v }))}
                {num('calm', t('target.f.calm'), target.calm_s, RANGES.calm, (v) => ({ calm_s: v }), 's', 0)}
                {num('alertR', t('target.f.alertRadius'), target.alert_radius_m, { min: 0, max: 1000 }, (v) => ({ alert_radius_m: v }), 'm', 0)}
                <span className="col-span-2 text-hud-sub text-muted-foreground">
                  {target.reaction ? t('target.reaction', { onset: t(`reaction.${target.reaction.onset?.kind ?? 'ignore'}`), response: (target.reaction.response ?? []).map((r) => t(`reaction.${r.kind}`)).join(t('common.listSep')) }) : '—'}
                </span>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>
      </SheetContent>
    </Sheet>
  )
}

export function TargetDetailPanel() {
  const t = useT()
  const id = useEntities((s) => s.target)
  const operator = useD2Write()
  const sandbox = useSurface((s) => s.surface === 'sandbox')
  // the configuration lives in the target model (details with cfg, REST seed, call results): re-read with the version
  useTargets((s) => s.version)
  const detail = useDetail(id)
  const patch = usePatch(id)
  // the open detail page is the store's selection: its detail topic runs at 2 Hz and the layer highlights it
  React.useEffect(() => {
    targetsActions.select(id)
  }, [id])
  const drawing = useD2Tool((s) => (s.mode === 'TARGET_PATH' || s.mode === 'TARGET_REGION' || s.mode === 'TARGET_MOVE' ? s.mode : null))
  const nPts = useD2Tool((s) => s.points.length)
  const [advanced, setAdvanced] = React.useState(false)
  // D2.1 (UX-FR-160): the "perception readout" tab next to the configuration
  const [tab, setTab] = React.useState<'config' | 'quant'>('config')
  const back = (
    <Button size="xs" variant="ghost" onClick={() => entities.back()} data-target-back="">
      <Icon icon="chev.left" data-icon="inline-start" />{t('targets.back')}
    </Button>
  )
  if (!id) return <div className="flex flex-col gap-2">{back}<PanelEmpty brand={false} title={t('targets.none')} /></div>
  const target = ((targetsModel.cfgs.get(id) ?? detail?.cfg ?? null) as Target | null)
  if (!target) return <div className="flex flex-col gap-2">{back}<p className="text-hud-sub text-muted-foreground">{t('common.loading')}</p></div>
  const cls = target.class
  const m = target.motion ?? { mode: 'static' as const }
  const remove = () => confirmThen({
    id: 'target.remove', title: t('targets.remove.title', { label: sanitizeText(target.label ?? id, 32) }), body: t('targets.remove.body'), action: t('targets.remove'),
    run: () => void targetsActions.remove(id).then((o) => {
      if (o.ok) entities.openTarget(null)
      else notify('targets:remove', 'warning', targetCallText(o))
    }),
  })
  const move = () => d2Tools.start('TARGET_MOVE', id, ([p]) => void patch({ pos: [p[0], p[1], cls === 'uav' ? p[2] + (target.alt_agl_m ?? 30) : null] }))
  const drawPath = () => d2Tools.start('TARGET_PATH', id, (pts) => void patch({ motion: { ...m, mode: 'path', path: pts.map((p) => [p[0], p[1], null]), loop: m.loop ?? 'loop' } }))
  const drawRegion = () => d2Tools.start('TARGET_REGION', id, (pts) => {
    const ring = pts.map((p) => [p[0], p[1]] as [number, number])
    void patch({ motion: { ...m, mode: 'random_walk', region: ring as unknown as NonNullable<Target['motion']>['region'] } })
  })
  return (
    <div className="flex min-h-0 min-w-0 flex-col gap-3 overflow-y-auto" data-target-detail={id}>
      <div className="flex items-center gap-1.5">
        {back}
        <Icon icon={CLASS_ICON[cls]} label={t(`target.class.${cls}`)} />
        <span className="truncate font-medium">{sanitizeText(target.label ?? id, 32)}</span>
        {operator ? (
          <span className="ml-auto flex gap-1">
            <Button size="xs" variant="outline" onClick={move} data-target-move="">{t('targets.move')}</Button>
            <Button size="xs" variant="ghost" onClick={remove} data-target-remove="">{t('targets.remove')}</Button>
          </span>
        ) : <Badge variant="outline" className="ml-auto">{t('role.shared')}</Badge>}
      </div>
      {drawing ? (
        <p className="flex items-center gap-1.5 text-hud-sub" data-target-drawing={drawing}>
          <Icon icon="mission.target" />
          {t(`tool.d2.${drawing}`)}
          {drawing !== 'TARGET_MOVE' ? <span className="text-muted-foreground">{t('tool.d2.points', { n: nPts })}</span> : null}
          {drawing !== 'TARGET_MOVE' ? <Button size="xs" variant="outline" className="ml-auto" onClick={() => d2Tools.commit()}>{t('tool.d2.commit')}</Button> : null}
          <Button size="xs" variant="ghost" className={drawing === 'TARGET_MOVE' ? 'ml-auto' : undefined} onClick={() => d2Tools.end()}>{t('common.cancel')}</Button>
        </p>
      ) : null}
      <LiveState id={id} />
      <Tabs value={tab} onValueChange={(v) => setTab(v === 'quant' ? 'quant' : 'config')} className="min-w-0">
        <TabsList variant="line" data-target-tabs={tab}>
          <TabsTrigger value="config" data-target-tab="config">{t('targets.tab.config')}</TabsTrigger>
          <TabsTrigger value="quant" data-target-tab="quant">{t('quant.title')}</TabsTrigger>
        </TabsList>
        <TabsPanels className="min-w-0">
          <TabsContent value="quant" className="min-w-0">
            <QuantCard targetId={id} targetLabel={sanitizeText(target.label ?? id, 32)} />
          </TabsContent>
          <TabsContent value="config" className="flex min-w-0 flex-col gap-3">
      <div className="grid grid-cols-2 gap-2" data-target-form="">
        <div className="col-span-2">
          <TextField id="label" label={t('target.f.label')} value={target.label ?? detail?.label ?? id} readOnly={!operator} validate={labelOk} onCommit={(v) => patch({ label: v })} />
        </div>
        <Field orientation="horizontal" className="col-span-2">
          <Switch checked={target.enabled !== false} disabled={!operator} id="tg-enabled" onCheckedChange={(on: boolean) => void patch({ enabled: on })} />
          <FieldLabel htmlFor="tg-enabled" className="font-normal">{t('target.f.enabled')}</FieldLabel>
        </Field>
        {(['l', 'w', 'h'] as const).map((k) => (
          <NumField key={k} id={`size-${k}`} label={t(`target.f.size.${k}`)} value={target.size_m?.[k]} unit="m" range={RANGES.size} readOnly={!operator}
            onCommit={(v) => patch({ size_m: { l: target.size_m?.l ?? 0.5, w: target.size_m?.w ?? 0.5, h: target.size_m?.h ?? 0.5, [k]: v } })} />
        ))}
        <Field className="col-span-2">
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('target.f.motion')}</FieldLabel>
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[m.mode]} disabled={!operator} aria-label={t('target.f.motion')} data-target-motion={m.mode}
            onValueChange={(v: unknown[]) => {
              const mode = v[0]
              if (mode === 'static') void patch({ motion: { ...m, mode } })
              else if (mode === 'path') drawPath()
              else if (mode === 'random_walk') drawRegion()
            }}>
            <ToggleGroupItem value="static">{t('target.motion.static')}</ToggleGroupItem>
            <ToggleGroupItem value="path">{t('target.motion.path')}</ToggleGroupItem>
            <ToggleGroupItem value="random_walk">{t('target.motion.random_walk')}</ToggleGroupItem>
          </ToggleGroup>
        </Field>
        <NumField id="speed" label={t('target.f.speed')} value={m.speed_mps} unit="m/s" range={RANGES.speed} readOnly={!operator}
          onCommit={(v) => patch({ motion: { ...m, speed_mps: v } })} />
        {m.mode === 'path' ? (
          <Field>
            <FieldLabel className="text-hud-sub text-muted-foreground">{t('target.f.loop')}</FieldLabel>
            <Select items={(['once', 'pingpong', 'loop'] as const).map((x) => ({ value: x, label: t(`target.loop.${x}`) }))} value={m.loop ?? 'loop'} disabled={!operator}
              onValueChange={(v) => (v === 'once' || v === 'pingpong' || v === 'loop') && void patch({ motion: { ...m, loop: v } })}>
              <SelectTrigger size="sm" className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>{(['once', 'pingpong', 'loop'] as const).map((x) => <SelectItem key={x} value={x}>{t(`target.loop.${x}`)}</SelectItem>)}</SelectContent>
            </Select>
          </Field>
        ) : <span />}
        <NumField id="contrast" label={t('target.f.contrast')} value={target.contrast0} range={RANGES.contrast} readOnly={!operator} onCommit={(v) => patch({ contrast0: v })} />
        <NumField id="dT" label={t('target.f.deltaT')} value={target.thermal?.delta_t_k} unit="K" range={RANGES.deltaT} digits={1} readOnly={!operator}
          onCommit={(v) => patch({ thermal: { ...(target.thermal ?? { mode: 'relative' }), mode: 'relative', delta_t_k: v } })} />
        <Field orientation="horizontal">
          <Switch checked={!!target.sound} disabled={!operator} id="tg-sound" onCheckedChange={(on: boolean) => void patch({
            sound: on ? { waveform: 'harmonic', f0_hz: 200, lw_dba: 70, period_s: 8, duty: 0.25, harmonics_db: [0, -6, -10, -14] } : null,
          })} />
          <FieldLabel htmlFor="tg-sound" className="font-normal">{t('target.f.sound')}</FieldLabel>
        </Field>
        {target.sound ? (
          <NumField id="lw" label={t('target.f.lw')} value={target.sound.lw_dba} unit="dB(A)" range={RANGES.lwDba} digits={0} readOnly={!operator}
            onCommit={(v) => patch({ sound: { ...(target.sound as Sound), lw_dba: v } })} />
        ) : <span />}
        <Field className="col-span-2">
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('target.f.criterion')}</FieldLabel>
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[target.criterion_mode ?? 'geometric']} disabled={!operator} aria-label={t('target.f.criterion')}
            onValueChange={(v: unknown[]) => (v[0] === 'geometric' || v[0] === 'physical') && void patch({ criterion_mode: v[0] })}>
            <ToggleGroupItem value="geometric">{t('criterion.geometric')}</ToggleGroupItem>
            <ToggleGroupItem value="physical">{t('criterion.physical')}</ToggleGroupItem>
          </ToggleGroup>
        </Field>
        <Field>
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('target.f.levelReq')}</FieldLabel>
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[target.level_req ?? 'R']} disabled={!operator} aria-label={t('target.f.levelReq')}
            onValueChange={(v: unknown[]) => (v[0] === 'D' || v[0] === 'R' || v[0] === 'I') && void patch({ level_req: v[0] })}>
            {(['D', 'R', 'I'] as const).map((l) => <ToggleGroupItem key={l} value={l}>{l}</ToggleGroupItem>)}
          </ToggleGroup>
        </Field>
        <NumField id="nreq" label={t('target.f.nReq')} value={target.n_req_px ?? null} unit="px" range={RANGES.nReq} digits={0} readOnly={!operator}
          onCommit={(v) => patch({ n_req_px: v })} />
      </div>
      <SensitivityEditor id={id} target={target} operator={operator} sandbox={sandbox} />
      <Button size="sm" variant="outline" onClick={() => setAdvanced(true)} data-target-advanced-open="">{t('target.advanced.open')}</Button>
          </TabsContent>
        </TabsPanels>
      </Tabs>
      <AdvancedSheet open={advanced} onOpenChange={setAdvanced} target={target} patch={patch} readOnly={!operator} />
    </div>
  )
}
