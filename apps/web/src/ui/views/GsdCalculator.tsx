// GSD and recognition-distance calculator Sheet (M15-FR-220; UX-FR-144; AWR-14 §20.13.5, §20.15.1 "GSD 计算器"): model and
// sensor (the reference GX40 day and night, LWIR 640 lenses and the catalogue's imaging sensors), analysis resolution,
// focal length (log slider over the zoom range), target class, aspect (level, top-down, worst), required level, slant
// range, light band and visibility MOR; the result block shows GSD, critical dimension and pixels on target, the TTPF
// probability per level, the largest slant range per level at P >= 0.9 and the feasible station window of the class with
// its limiting reason. Instant values recompute at most 10 Hz while a control moves; the window once the inputs settle.
// Opened from the tools menu, the command palette, the GSD scan form and the catalogue. Pure local computation (viewer
// safe, no request); "the criterion of record is the server's perception" is stated under the results.
import * as React from 'react'
import { useStore } from 'zustand'
import { tOr, useT } from '@/app/i18n'
import { createAwrStore } from '@/lib/createStore'
import { fmt } from '@/lib/format'
import { catalog, useCatalog } from '@/stores/catalog'
import { TARGET_CLASSES, type Light, type ReqLevel, type TargetClassName } from '@/app/shell/engineD2'
import { Field, FieldDescription, FieldLabel } from '@/ui/components/ui/field'
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from '@/ui/components/ui/input-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Separator } from '@/ui/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Slider } from '@/ui/components/ui/slider'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { LfStat } from '@/ui/lf/LfStat'
import { calc, MOR_OPTIONS, posZoom, sensorOptions, windowFor, zoomPos, type Aspect, type Resolution } from './gsdModel'

export const gsdCalcStore = createAwrStore<{ open: boolean; sensor: string | null }>('ui.gsdCalc', () => ({ open: false, sensor: null }))
export const gsdCalc = {
  open(sensor: string | null = null): void {
    gsdCalcStore.setState({ open: true, sensor })
  },
  close(): void {
    gsdCalcStore.setState({ open: false })
  },
}

/** a value that follows `v` at most every `ms` (the last change always lands) */
function useThrottled<T>(v: T, ms: number): T {
  const [out, setOut] = React.useState(v)
  const last = React.useRef(0)
  React.useEffect(() => {
    const now = performance.now()
    const wait = Math.max(0, ms - (now - last.current))
    const id = setTimeout(() => {
      last.current = performance.now()
      setOut(v)
    }, wait)
    return () => clearTimeout(id)
  }, [v, ms])
  return out
}

const LEVELS: readonly ReqLevel[] = ['D', 'R', 'I']

function Toggle<V extends string>({ label, value, items, onChange, id }: { label: string; value: V; items: readonly { v: V; label: string }[]; onChange: (v: V) => void; id: string }) {
  return (
    <Field>
      <FieldLabel className="text-hud-sub text-muted-foreground">{label}</FieldLabel>
      <ToggleGroup spacing={0} size="sm" variant="outline" value={[value]} aria-label={label} data-gsd-toggle={id}
        onValueChange={(v: unknown[]) => items.some((x) => x.v === v[0]) && onChange(v[0] as V)}>
        {items.map((x) => <ToggleGroupItem key={x.v} value={x.v}>{x.label}</ToggleGroupItem>)}
      </ToggleGroup>
    </Field>
  )
}

function CalcBody() {
  const t = useT()
  const sensorsCat = useCatalog((s) => s.sensors)
  const want = useStore(gsdCalcStore, (s) => s.sensor)
  React.useEffect(() => {
    if (catalog && sensorsCat.length === 0) void catalog.load({ sensors: true })
  }, [sensorsCat.length])
  const options = React.useMemo(() => sensorOptions(sensorsCat), [sensorsCat])
  const [sensorId, setSensorId] = React.useState<string>(want && options.some((o) => o.id === want) ? want : 'ref:gx40')
  const opt = options.find((o) => o.id === sensorId) ?? options[0]
  const [res, setRes] = React.useState<Resolution>('1080p')
  const sensor = React.useMemo(() => opt.make(res), [opt, res])
  const [pos, setPos] = React.useState(1000)
  const fMm = opt.zoom ? posZoom(pos, sensor.fMinMm, sensor.fMaxMm) : sensor.fMaxMm
  const [cls, setCls] = React.useState<TargetClassName>('person')
  const [aspect, setAspect] = React.useState<Aspect>('level')
  const [level, setLevel] = React.useState<ReqLevel>('R')
  const [range, setRange] = React.useState('600')
  const [light, setLight] = React.useState<Light>(opt.kind === 'eo' ? 'day' : 'night')
  const [mor, setMor] = React.useState(10000)
  const rangeM = Math.min(20000, Math.max(1, Number(range) || 600))
  const input = React.useMemo(() => ({ sensor, fMm, resolution: res, cls, aspect, level, rangeM, light, morM: mor }), [sensor, fMm, res, cls, aspect, level, rangeM, light, mor])
  const live = useThrottled(input, 100)
  const out = React.useMemo(() => calc(live), [live])
  const settled = useThrottled(input, 400)
  const win = React.useMemo(() => windowFor(settled), [settled])
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 pb-4" data-gsd-calc="">
      <div className="grid grid-cols-2 gap-2">
        <Field className="col-span-2">
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('gsd.sensor')}</FieldLabel>
          <Select items={options.map((o) => ({ value: o.id, label: o.label }))} value={opt.id} onValueChange={(v) => {
            if (typeof v !== 'string') return
            setSensorId(v)
            const o = options.find((x) => x.id === v)
            if (o) setLight(o.kind === 'eo' ? 'day' : 'night')
          }}>
            <SelectTrigger size="sm" className="w-full" data-gsd-sensor={opt.id}><SelectValue /></SelectTrigger>
            <SelectContent>{options.map((o) => <SelectItem key={o.id} value={o.id}>{`${o.label} · ${t(`gsd.kind.${o.kind}`)}`}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
        <Toggle id="res" label={t('gsd.resolution')} value={res} items={[{ v: '1080p', label: '1080P' }, { v: '4k', label: '4K' }]} onChange={setRes} />
        <Toggle id="level" label={t('gsd.level')} value={level} items={LEVELS.map((l) => ({ v: l, label: t(`perception.best.${l}`) }))} onChange={setLevel} />
        <Field className="col-span-2">
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('gsd.focal')}</FieldLabel>
          <div className="flex items-center gap-2">
            <span className="font-mono text-hud-sub text-muted-foreground">{fmt.num(sensor.fMinMm, 1)}</span>
            <Slider className="flex-1" min={0} max={1000} step={1} disabled={!opt.zoom} value={[opt.zoom ? pos : zoomPos(fMm, sensor.fMinMm, sensor.fMaxMm)]}
              aria-label={t('gsd.focal')} data-gsd-focal="" onValueChange={(v) => setPos(Array.isArray(v) ? (v as number[])[0] : (v as number))} />
            <span className="font-mono text-hud-sub text-muted-foreground">{fmt.num(sensor.fMaxMm, 1)}</span>
            <span className="w-16 text-right font-mono text-hud-sub" data-gsd-f={fmt.num(fMm, 1)}>{`${fmt.num(fMm, 1)} mm`}</span>
          </div>
        </Field>
        <Field>
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('gsd.class')}</FieldLabel>
          <Select items={TARGET_CLASSES.map((c) => ({ value: c, label: t(`target.class.${c}`) }))} value={cls} onValueChange={(v) => typeof v === 'string' && setCls(v as TargetClassName)}>
            <SelectTrigger size="sm" className="w-full" data-gsd-class=""><SelectValue /></SelectTrigger>
            <SelectContent>{TARGET_CLASSES.map((c) => <SelectItem key={c} value={c}>{t(`target.class.${c}`)}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel htmlFor="gsd-range" className="text-hud-sub text-muted-foreground">{t('gsd.range')}</FieldLabel>
          <InputGroup>
            <InputGroupInput id="gsd-range" inputMode="decimal" value={range} onChange={(e) => setRange(e.target.value)} data-gsd-range="" />
            <InputGroupAddon align="inline-end"><InputGroupText>m</InputGroupText></InputGroupAddon>
          </InputGroup>
        </Field>
        <Toggle id="aspect" label={t('gsd.aspect')} value={aspect} items={(['level', 'down', 'worst'] as const).map((v) => ({ v, label: t(`gsd.aspect.${v}`) }))} onChange={setAspect} />
        <Toggle id="light" label={t('gsd.light')} value={light} items={(['day', 'low', 'night'] as const).map((v) => ({ v, label: t(`sandbox.band.${v}`) }))} onChange={setLight} />
        <Field className="col-span-2">
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('gsd.mor')}</FieldLabel>
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[String(mor)]} aria-label={t('gsd.mor')} onValueChange={(v: unknown[]) => v[0] && setMor(Number(v[0]))}>
            {MOR_OPTIONS.map((m) => <ToggleGroupItem key={m} value={String(m)}>{`${fmt.num(m / 1000, 0)} km`}</ToggleGroupItem>)}
          </ToggleGroup>
        </Field>
      </div>
      <Separator />
      <div className="grid grid-cols-3 gap-2" data-gsd-result="">
        <LfStat label={t('gsd.r.gsd')} unit="cm" value={out.gsdCm} format={(v) => fmt.num(v, v < 10 ? 2 : 1)} />
        <LfStat label={t('gsd.r.dc')} unit="m" value={out.dcM} format={(v) => fmt.num(v, 2)} />
        <LfStat label={t('gsd.r.n')} unit="px" value={out.nEff} format={(v) => fmt.num(v, 1)} />
      </div>
      <p className="font-mono text-hud-sub" data-gsd-p="">
        {LEVELS.map((L) => `P(${L}) ${fmt.num(out.p[L], 2)}`).join(' · ')}
        {out.limiting ? ` · ${t('perception.limitedBy', { factor: t(`perception.limiting.${out.limiting}`) })}` : ''}
      </p>
      <p className="font-mono text-hud-sub" data-gsd-rmax="">
        {t('gsd.r.rmax', { d: fmt.num(out.rMax.D, 0), r: fmt.num(out.rMax.R, 0), i: fmt.num(out.rMax.I, 0) })}
      </p>
      <p className="text-hud-sub" data-gsd-window={win.why ?? 'ok'}>
        {win.why === null && win.dLoM !== null && win.dHiM !== null
          ? t('gsd.r.window', { lo: fmt.num(win.dLoM, 0), hi: fmt.num(win.dHiM, 0), agl: fmt.num(win.aglM ?? Number.NaN, 0), level })
          : t('gsd.r.noWindow', { level, why: tOr(`tasking.why.${win.why ?? 'pixels'}`, win.why ?? '') })}
      </p>
      <FieldDescription>{t('gsd.note')}</FieldDescription>
    </div>
  )
}

export function GsdCalculatorSheet() {
  const t = useT()
  const open = useStore(gsdCalcStore, (s) => s.open)
  return (
    <Sheet open={open} onOpenChange={(o) => (o ? undefined : gsdCalc.close())}>
      <SheetContent side="right" className="w-120 max-w-full sm:max-w-120" data-gsd-sheet="">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2"><Icon icon="perf.chart" />{t('gsd.title')}</SheetTitle>
          <SheetDescription>{t('gsd.desc')}</SheetDescription>
        </SheetHeader>
        {open ? <CalcBody /> : null}
      </SheetContent>
    </Sheet>
  )
}
