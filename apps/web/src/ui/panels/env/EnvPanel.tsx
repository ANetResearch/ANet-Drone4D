// ENVIRONMENT group (M15-FR-021, FR-027; AWR-14 §4.2, §6.14; M07 §8.1): the 12 presets as a 3 x 4 ToggleGroup (grid,
// no sliding indicator; a clicked preset shows a pending ring until the keyframe's toPreset confirms it, then the
// transition Progress "12 / 30 s" in simulation seconds), reference wind speed (with the Beaufort badge), wind direction
// (the env.wind.dir icon rotates with CSS rotate to the downwind direction, unwrapped so 359 -> 1 turns +2 degrees),
// background visibility (log slider 50 m - 50 km; the main number is the total MOR), precipitation (rain or snow) and
// cloud cover, plus the selected vehicle's local environment. Sliders show a local draft while dragged and send one
// env/set on release (keyboard changes after INPUT.sliderCommitIdleMs); the draft keeps a pending outline until the
// server state follows; a rejection rolls the draft back and shakes the field once. The environment is server-authored
// (U-03): viewers and replay see the values read-only. Values come from stores/env (M07).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { INPUT } from '@/lib/tokens/input.gen'
import { Alert, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Progress } from '@/ui/components/ui/progress'
import { Slider } from '@/ui/components/ui/slider'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { LfStat } from '@/ui/lf/LfStat'
import { ShakeOnce } from '@/ui/motion/ShakeOnce'
import { useCmdEntry } from '@/ui/actions/commands'
import { runService } from '@/ui/actions/vehicleCommands'
import { useConnView } from '@/ui/shell/connView'
import { writeDeniedKey } from '@/ui/shell/guards'
import { envStore, useEnv, type PresetId } from '@/stores/env'

export const PRESETS: readonly { id: PresetId; icon: IconKey }[] = [
  { id: 'clear', icon: 'env.clear' }, { id: 'partlyCloudy', icon: 'env.partly' }, { id: 'overcast', icon: 'env.overcast' },
  { id: 'lightRain', icon: 'env.drizzle' }, { id: 'rain', icon: 'env.rain' }, { id: 'heavyRain', icon: 'env.storm' },
  { id: 'thunderstorm', icon: 'env.thunder' }, { id: 'fog', icon: 'env.fog' }, { id: 'haze', icon: 'env.haze' },
  { id: 'snow', icon: 'env.snow' }, { id: 'blizzard', icon: 'env.blizzard' }, { id: 'sandstorm', icon: 'env.sand' },
]
const MOR_LO = Math.log10(50)
const MOR_HI = Math.log10(50000)
const PRESET_DURATION_S = 30
const SET_DURATION_S = 3

/** wire name of a preset (env/preset {name}): the ids of packages/contracts/env/presets.json */
export const presetWireName = (id: PresetId): string => id

/** unwrapped rotation: the shortest turn from prev to next (AWR-15 §7.5 with the negative-modulo fix, M15 §14 item 16) */
export function unwrapDeg(prev: number, next: number): number {
  const d = ((((next - prev) % 360) + 540) % 360) - 180
  return prev + d
}

interface Draft { v: number; sentAt: number }
/** one env field with a local draft between release and confirmation */
function useEnvField(key: string, server: number, tol: number, send: (v: number) => void) {
  const [draft, setDraft] = React.useState<Draft | null>(null)
  const [dragging, setDragging] = React.useState<number | null>(null)
  const e = useCmdEntry(`env:${key}`)
  const kbd = React.useRef<ReturnType<typeof setTimeout> | null>(null)
  // confirmation: the server value follows the draft, or the call failed (roll back), or it succeeded long ago
  if (draft && ((Number.isFinite(server) && Math.abs(server - draft.v) <= tol) || e?.phase === 'DONE_ERR')) setDraft(null)
  React.useEffect(() => {
    if (!draft || e?.phase !== 'DONE_OK') return
    const h = setTimeout(() => setDraft(null), INPUT.resultHoldMs * 2)
    return () => clearTimeout(h)
  }, [draft, e?.phase])
  React.useEffect(() => () => {
    if (kbd.current) clearTimeout(kbd.current)
  }, [])
  const commit = (v: number, keyboard: boolean) => {
    setDragging(null)
    const go = () => {
      setDraft({ v, sentAt: performance.now() })
      send(v)
    }
    if (kbd.current) clearTimeout(kbd.current)
    if (keyboard) kbd.current = setTimeout(go, INPUT.sliderCommitIdleMs)
    else go()
  }
  const value = dragging ?? draft?.v ?? server
  return { value, pending: draft !== null, shake: e?.shake ?? 0, onChange: (v: number) => setDragging(v), commit }
}

const isKey = (d: unknown): boolean => {
  const ev = (d as { event?: Event } | undefined)?.event
  return typeof KeyboardEvent !== 'undefined' && ev instanceof KeyboardEvent
}
const first = (v: number | readonly number[]): number => (Array.isArray(v) ? (v[0] ?? 0) : (v as number))

function EnvSlider({ id, label, icon, iconStyle, valueText, min, max, step, field, disabled, extra }: {
  id: string; label: string; icon: IconKey; iconStyle?: React.CSSProperties; valueText: string; min: number; max: number; step: number
  field: ReturnType<typeof useEnvField>; disabled: boolean; extra?: React.ReactNode
}) {
  return (
    <ShakeOnce trigger={field.shake}>
      <div data-env-field={id} data-pending={field.pending ? '' : undefined}>
        <Field>
          <FieldLabel className="justify-between font-normal">
            <span className="inline-flex items-center gap-1.5"><Icon icon={icon} style={iconStyle} data-rotate-icon={iconStyle ? '' : undefined} />{label}</span>
            <span data-numeric="" className={cn('shrink-0 rounded-sm px-1', field.pending && 'ring-1 ring-ring')}>{valueText}</span>
          </FieldLabel>
          <Slider value={[Number.isFinite(field.value) ? field.value : min]} min={min} max={max} step={step} disabled={disabled} aria-label={label}
            onValueChange={(v) => field.onChange(first(v))} onValueCommitted={(v, d) => field.commit(first(v), isKey(d))} />
          {/* secondary facts under the slider, so the label row never wraps in the 288 px rail */}
          {extra ? <span className="text-hud-sub">{extra}</span> : null}
        </Field>
      </div>
    </ShakeOnce>
  )
}

function setEnv(key: string, patch: Record<string, unknown>, what: string): void {
  runService(`env:${key}`, 'env/set', { patch, duration_s: SET_DURATION_S }, what)
}

/**
 * readings at the selected vehicle (written at the UI tick, 4 Hz on Tier S): their own component, so a new reading
 * re-renders four stats instead of the whole panel with its six sliders and the preset grid (ADR-069; D1-AC-27)
 */
function EnvLocalReadings() {
  const t = useT()
  const selected = useEnv((s) => s.selected)
  if (!selected) return null
  return (
    <div className="grid grid-cols-2 gap-2 border-t pt-2" data-env-local="">
      <LfStat label={t('env.local.wind')} value={selected.windMps} format={(v) => fmt.num(v, 1)} unit="m/s" />
      <LfStat label={t('env.local.gust')} value={selected.gustMps} format={(v) => fmt.num(v, 1)} unit="m/s" />
      <LfStat label={t('env.local.mor')} value={selected.morM} format={fmt.mor} />
      <LfStat label={t('env.local.airspeed')} value={selected.airspeedMps} format={(v) => fmt.num(v, 1)} unit="m/s" />
      {selected.stale ? <Badge variant="outline" className="col-span-2 border-dashed">{t('env.stale')}</Badge> : null}
    </div>
  )
}

export function EnvPanel() {
  const t = useT()
  const active = useEnv((s) => s.activePreset)
  const toPreset = useEnv((s) => s.toPreset)
  const sc = useEnv((s) => s.scalars)
  const derived = useEnv((s) => s.derived)
  const beaufort = useEnv((s) => s.beaufort)
  const transition = useEnv((s) => s.transition)
  const hashOk = useEnv((s) => s.presetHashOk)
  useConnView((s) => s.version)
  const denied = writeDeniedKey()
  const ro = denied !== null
  const [clicked, setClicked] = React.useState<PresetId | null>(null)
  const presetEntry = useCmdEntry('env:preset')
  if (clicked && (toPreset === clicked || active === clicked || presetEntry?.phase === 'DONE_ERR')) setClicked(null)
  const [precip, setPrecip] = React.useState<'rain' | 'snow'>('rain')
  const [rot, setRot] = React.useState({ src: Number.NaN, deg: 0 })
  const wind = useEnvField('wind', sc.windSpeedRefMps, 0.05, (v) => setEnv('wind', { wind: { speed_ref_mps: v } }, t('env.windSpeed')))
  const dir = useEnvField('dir', sc.windDirFromDeg, 0.5, (v) => setEnv('dir', { wind: { dir_from_deg: v } }, t('env.windDir')))
  const mor = useEnvField('mor', Number.isFinite(sc.morBgM) ? Math.log10(Math.max(1, sc.morBgM)) : Number.NaN, 0.005,
    (v) => setEnv('mor', { atmosphere: { mor_bg_m: Math.round(10 ** v) } }, t('env.morBg')))
  const rain = useEnvField('rain', sc.rainMmh, 0.05, (v) => setEnv('rain', { precip: { rain_mmh: v } }, t('env.rain')))
  const snow = useEnvField('snow', sc.snowMmh, 0.05, (v) => setEnv('snow', { precip: { snow_mmh: v } }, t('env.snow')))
  const cloud = useEnvField('cloud', Number.isFinite(sc.cloudCover) ? sc.cloudCover * 100 : Number.NaN, 0.5, (v) => setEnv('cloud', { cloud: { cover: v / 100 } }, t('env.cloud')))
  // the icon points downwind (from + 180); the angle is unwrapped so 359 -> 1 turns +2 degrees (derived during render)
  if (Number.isFinite(dir.value) && dir.value !== rot.src) setRot({ src: dir.value, deg: unwrapDeg(rot.deg, dir.value + 180) })
  const pField = precip === 'rain' ? rain : snow
  const elapsed = transition.active ? Math.max(0, transition.progress * (transition.t1SimS - transition.t0SimS)) : 0
  const total = transition.t1SimS - transition.t0SimS
  const effNote = precip === 'rain' && Number.isFinite(derived.rainEffMmh) && derived.rainEffMmh < (sc.rainMmh || 0) - 0.05
  return (
    <div className="flex flex-col gap-2" data-env-panel="" data-readonly={ro ? '' : undefined}>
      {!hashOk ? (
        <Alert><Icon icon="alert.warning" /><AlertDescription>{t('env.hashMismatch')}</AlertDescription></Alert>
      ) : null}
      {ro ? <p className="text-hud-sub text-muted-foreground">{t(denied)}</p> : null}
      <ToggleGroup indicator={false} spacing={1} size="sm" variant="outline" value={active ? [active] : []} disabled={ro}
        aria-label={t('env.presets')} className="grid w-full grid-cols-3"
        onValueChange={(v: unknown[]) => {
          const id = (v.length ? v[v.length - 1] : active) as PresetId | null
          if (!id || id === active) return
          setClicked(id)
          runService('env:preset', 'env/preset', { name: presetWireName(id), duration_s: PRESET_DURATION_S }, t(`env.preset.${id}`))
        }}>
        {PRESETS.map((p) => (
          <ToggleGroupItem key={p.id} value={p.id} aria-label={t(`env.preset.${p.id}`)} data-preset={p.id} data-pending={clicked === p.id ? '' : undefined}
            className={cn('justify-start', clicked === p.id && 'ring-1 ring-ring')}>
            <Icon icon={p.icon} data-icon="inline-start" />
            <span className="truncate">{t(`env.preset.${p.id}`)}</span>
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      {transition.active ? (
        <div className="flex items-center gap-2 text-hud-sub" data-env-transition="">
          <Progress value={transition.progress * 100} className="flex-1" aria-label={t('env.transition')} />
          <span className="font-mono">{t('env.transitionS', { t: fmt.num(elapsed), total: fmt.num(total) })}</span>
        </div>
      ) : null}
      <EnvSlider id="wind" label={t('env.windSpeed')} icon="env.wind" valueText={fmt.speed(wind.value)} min={0} max={40} step={0.5} field={wind} disabled={ro}
        extra={beaufort > 0 ? <Badge variant="outline">{t('env.beaufort', { n: beaufort })}</Badge> : null} />
      <EnvSlider id="dir" label={t('env.windDir')} icon="env.wind.dir" iconStyle={{ rotate: `${rot.deg}deg` }}
        valueText={Number.isFinite(dir.value) ? `${t(`compass.${Math.round((((dir.value % 360) + 360) % 360) / 45) % 8}`)} ${fmt.dirDeg(dir.value)}` : fmt.dirDeg(dir.value)}
        min={0} max={360} step={5} field={dir} disabled={ro} />
      <EnvSlider id="mor" label={t('env.morBg')} icon="env.visibility" valueText={fmt.mor(Number.isFinite(mor.value) ? 10 ** mor.value : Number.NaN)}
        min={MOR_LO} max={MOR_HI} step={0.01} field={mor} disabled={ro}
        extra={<span className="text-muted-foreground">{t('env.morTotal', { mor: fmt.mor(derived.morM) })}</span>} />
      <Field>
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[precip]} aria-label={t('env.precipKind')}
          onValueChange={(v: unknown[]) => v[0] !== undefined && setPrecip(v[0] as 'rain' | 'snow')}>
          <ToggleGroupItem value="rain">{t('env.rain')}</ToggleGroupItem>
          <ToggleGroupItem value="snow">{t('env.snow')}</ToggleGroupItem>
        </ToggleGroup>
      </Field>
      <EnvSlider id={precip} label={t(precip === 'rain' ? 'env.rain' : 'env.snow')} icon={precip === 'rain' ? 'env.rain' : 'env.snow'}
        valueText={fmt.mmh(pField.value)} min={0} max={precip === 'rain' ? 150 : 30} step={0.5} field={pField} disabled={ro}
        extra={effNote ? <span className="text-muted-foreground">{t('env.rainEff', { v: fmt.mmh(derived.rainEffMmh) })}</span> : null} />
      <EnvSlider id="cloud" label={t('env.cloud')} icon="env.cloud" valueText={fmt.pct(cloud.value)} min={0} max={100} step={1} field={cloud} disabled={ro} />
      <EnvLocalReadings />
    </div>
  )
}

/** store read for tests and the command palette (current preset) */
export const currentPreset = (): PresetId | null => envStore.getState().activePreset
