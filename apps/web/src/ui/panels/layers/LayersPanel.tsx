// LAYERS group (M15-FR-021; AWR-14 §4.2, §6.15): one Switch per layer (Field horizontal, layer icon), colour mode
// ToggleGroup with the sliding indicator, point classes (anet-classes@1 bits 0-15 as Checkboxes; changing them never
// reloads data, ADR-005), quality rung Select (automatic or one of the 7 rungs, M05 LADDER) with the live point budget
// (drawn points against the rung band, 20-tick mini gauge, limitedBy), and EDL (Tier B/A; greyed on Tier S). Values go
// straight to stores/layers (M05), never React state.
import { useT } from '@/app/i18n'
import { LADDER } from '@/engine'
import { fmt } from '@/lib/format'
import { Checkbox } from '@/ui/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/ui/components/ui/collapsible'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Switch } from '@/ui/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { LfTickGauge } from '@/ui/lf/LfTickGauge'
import { layers, useLayers, type ColorMode, type LayerId, type QualityMode } from '@/stores/layers'
import { usePerf } from '@/stores/perf'
import { useWorld } from '@/stores/world'

const ROWS: readonly { id: LayerId; icon: IconKey }[] = [
  { id: 'pointcloud', icon: 'layer.pointcloud' }, { id: 'drones', icon: 'drone.quad' }, { id: 'trails', icon: 'layer.trajectory' },
  { id: 'frustums', icon: 'cam.fov' }, { id: 'mission', icon: 'mission.list' }, { id: 'zones', icon: 'zone.nofly' },
  { id: 'environment', icon: 'nav.environment' }, { id: 'labels', icon: 'layer.semantic' },
]
const MODES: readonly ColorMode[] = ['height', 'hag', 'normal', 'class']
const CLASSES = 16

/**
 * the live point budget under the quality Select: the only part of the group that follows the engine statistics (4 Hz),
 * so a statistics write re-renders these lines and not the eight layer Switches, the colour ToggleGroup and the Select
 * (P4-UI, D1-AC-23: every re-render of a Base UI Switch rewrote its hidden input's attributes)
 */
function LayersBudget() {
  const t = useT()
  const B = useWorld((s) => s.B)
  const drawn = useWorld((s) => s.drawn)
  const hi = useWorld((s) => s.hi)
  const rung = useWorld((s) => s.rung)
  const limitedBy = useWorld((s) => s.limitedBy)
  const budget = B > 0 ? B : hi > 0 ? hi : (LADDER[rung.index]?.hi ?? 0)
  const pct = budget > 0 ? (drawn / budget) * 100 : 0
  return (
    <>
      <div className="flex items-center justify-between gap-2 text-hud-sub" data-budget="">
        <span className="truncate font-mono tabular-nums">{t('layers.budget', { drawn: fmt.pts(drawn), b: fmt.pts(budget) })}</span>
        <LfTickGauge mini value={Math.min(100, pct)} ariaLabel={t('layers.budgetGauge')} />
      </div>
      <p className="text-hud-sub text-muted-foreground"><span className="font-mono">{LADDER[rung.index]?.name ?? rung.name}</span>{` · ${t(`limitedBy.${limitedBy}`)}`}</p>
    </>
  )
}

/** one layer switch: subscribes to its own visibility only (a colour mode or quality change re-renders none of them) */
function LayerSwitch({ id, icon }: { id: LayerId; icon: IconKey }) {
  const t = useT()
  const on = useLayers((s) => s.visible[id])
  return (
    <Field orientation="horizontal" className="justify-between">
      <FieldLabel htmlFor={`layer-${id}`} className="gap-1.5 font-normal">
        <Icon icon={icon} />
        {t(`layers.${id}`)}
      </FieldLabel>
      <Switch id={`layer-${id}`} checked={on} onCheckedChange={(v: boolean) => layers.setVisible(id, v)} />
    </Field>
  )
}

/** the point colour mode (D1-AC-25 "colour mode" operation): its own subscription, so the switch rows stay as they are */
function ColorModeGroup() {
  const t = useT()
  const colorMode = useLayers((s) => s.colorMode)
  return (
    <Field>
      <FieldLabel className="text-hud-cap uppercase text-muted-foreground">{t('layers.colorMode')}</FieldLabel>
      <ToggleGroup spacing={0} size="sm" variant="outline" value={[colorMode]} aria-label={t('layers.colorMode')}
        onValueChange={(v: unknown[]) => v[0] !== undefined && layers.setColorMode(v[0] as ColorMode)}>
        {MODES.map((m) => <ToggleGroupItem key={m} value={m}>{t(`layers.mode.${m}`)}</ToggleGroupItem>)}
      </ToggleGroup>
    </Field>
  )
}

function ClassMask() {
  const t = useT()
  const classMask = useLayers((s) => s.classMask)
  return (
    <Collapsible>
      <CollapsibleTrigger render={<Button size="xs" variant="ghost" className="w-full justify-between" />}>
        {t('layers.classes')}
        <Icon icon="chev.down" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="grid grid-cols-2 gap-1 pt-1" data-class-mask={classMask}>
          {Array.from({ length: CLASSES }, (_, i) => (
            <Field key={i} orientation="horizontal" className="gap-1.5">
              <Checkbox id={`class-${i}`} checked={(classMask & (1 << i)) !== 0}
                onCheckedChange={(on: boolean) => layers.setClassMask(on ? classMask | (1 << i) : classMask & ~(1 << i))} />
              <FieldLabel htmlFor={`class-${i}`} className="truncate font-normal">{t(`layers.class.${i}`)}</FieldLabel>
            </Field>
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

function QualityField() {
  const t = useT()
  const quality = useLayers((s) => s.quality)
  const clamped = useWorld((s) => s.clampedByCapacity)
  const qualityItems = [{ value: 'auto', label: t('layers.quality.auto') }, ...LADDER.map((r, i) => ({ value: String(i), label: `${r.name} · ${t(`layers.rung.${i}`)}` }))]
  return (
    <Field>
      <FieldLabel className="justify-between text-hud-cap uppercase text-muted-foreground">
        {t('layers.quality')}
        {clamped ? <span className="normal-case">{t('limitedBy.pool')}</span> : null}
      </FieldLabel>
      <Select items={qualityItems} value={String(quality)} onValueChange={(v) => layers.setQuality(v === 'auto' || v === null ? 'auto' : (Number(v) as QualityMode))}>
        <SelectTrigger size="sm" className="w-full" aria-label={t('layers.quality')}><SelectValue /></SelectTrigger>
        <SelectContent>{qualityItems.map((q) => <SelectItem key={q.value} value={q.value}>{q.label}</SelectItem>)}</SelectContent>
      </Select>
      <LayersBudget />
    </Field>
  )
}

function EdlSwitch() {
  const t = useT()
  const edl = useLayers((s) => s.edl)
  const tier = usePerf((s) => s.tier)
  return (
    <Field orientation="horizontal" className="justify-between">
      <FieldLabel htmlFor="layer-edl" className="gap-1.5 font-normal">
        <Icon icon="layer.lod" />
        {tier === 'S' ? t('layers.edlSoft') : t('layers.edl')}
      </FieldLabel>
      <Switch id="layer-edl" checked={edl && tier !== 'S'} disabled={tier === 'S'} onCheckedChange={(on: boolean) => layers.setEdl(on)} />
    </Field>
  )
}

/**
 * the group is a set of independently subscribed parts (P4-UI, D1-AC-23 and 25): an engine statistics write, a colour
 * mode change or one layer switch re-renders only its own part; every re-render of a Base UI Switch rewrote its hidden
 * input's attributes
 */
export function LayersPanel() {
  return (
    <div className="flex flex-col gap-2">
      {ROWS.map((r) => <LayerSwitch key={r.id} id={r.id} icon={r.icon} />)}
      <ColorModeGroup />
      <ClassMask />
      <QualityField />
      <EdlSwitch />
    </div>
  )
}
