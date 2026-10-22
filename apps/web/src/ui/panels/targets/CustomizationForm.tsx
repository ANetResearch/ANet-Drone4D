import * as React from 'react'
import type { Target } from '@awr/contracts/types'
import { getLocale, useT } from '@/app/i18n'
import { Button } from '@/ui/components/ui/button'
import { Input } from '@/ui/components/ui/input'
import { Textarea } from '@/ui/components/ui/textarea'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { AppearancePreviewCard } from './AppearancePreviewCard'
import { customizationAssets, customizationErrors, customizationOf, customizationPatch, type Customization } from './customization'

/** Explicit save: switching models and entering a plate are submitted together, never on blur. */
export function CustomizationForm({ target, cls, operator, creating = false, save }: {
  target: Partial<Target>; cls: Target['class']; operator: boolean; creating?: boolean
  save: (patch: Partial<Target>) => Promise<boolean>
}) {
  const t = useT()
  const zh = getLocale() === 'zh-CN'
  const prefix = React.useId()
  const [value, setValue] = React.useState(() => customizationOf(target, cls, creating))
  const [busy, setBusy] = React.useState(false)
  const [saved, setSaved] = React.useState(false)
  const baseline = customizationOf(target, cls, creating)
  const dirty = JSON.stringify(value) !== JSON.stringify(baseline)
  const errors = customizationErrors(cls, value)
  const entries = customizationAssets(cls)
  const asset = entries.find((a) => a.id === value.assetId)
  const title = cls === 'person' ? (t('targets.customizationForm.person_appearance')) : cls === 'vehicle' ? (t('targets.customizationForm.vehicle_model')) : cls === 'dog' ? (t('targets.customizationForm.dog_appearance')) : (t('targets.customizationForm.appearance'))
  const items = [{ value: '', label: t('targets.customizationForm.original_wireframe') }, ...entries.map((a) => ({ value: a.id, label: zh ? a.name_zh : a.name_en }))]
  const edit = (key: keyof Customization, text: string) => { setValue((v) => ({ ...v, [key]: text })); setSaved(false) }
  const field = (key: keyof Customization, label: string, limit: number) => <Field>
    <FieldLabel htmlFor={`${prefix}-${key}`}>{label}</FieldLabel>
    <Input id={`${prefix}-${key}`} value={value[key]} maxLength={limit} disabled={!operator || busy}
      aria-invalid={errors.includes(key)} onChange={(e) => edit(key, e.target.value)} />
  </Field>
  const submit = async () => {
    if (errors.length || busy) return
    setBusy(true); setSaved(false)
    try { setSaved(await save(customizationPatch(cls, value, target))) }
    finally { setBusy(false) }
  }
  return <div className="flex min-w-0 flex-col gap-3" data-target-customization={cls}>
    {field('label', t('targets.customizationForm.target_name'), 32)}
    <Field>
      <FieldLabel>{title}</FieldLabel>
      <Select items={items} value={value.assetId} disabled={!operator || busy} onValueChange={(v) => typeof v === 'string' && edit('assetId', v)}>
        <SelectTrigger className="w-full" aria-label={title}><SelectValue /></SelectTrigger>
        <SelectContent>{items.map((a) => <SelectItem key={a.value} value={a.value}>{a.label}</SelectItem>)}</SelectContent>
      </Select>
      <p className="text-hud-sub text-muted-foreground">{t('targets.customizationForm.mesh_presets_selection_uses_the_model_s', { value1: entries.length })}</p>
      {entries.length === 1 ? <p className="text-hud-sub text-muted-foreground">{t('targets.customizationForm.one_mesh_is_available_separate_breed_coat')}</p> : null}
      {asset ? <p className="text-hud-sub text-muted-foreground">{asset.dimensions_m.map((n) => n.toFixed(2)).join(' × ')} m · {asset.license}</p> : null}
    </Field>
    <AppearancePreviewCard key={value.assetId} assetId={value.assetId} plate={value.plate} life={operator ? target.life : undefined} />
    {cls === 'vehicle' ? <>
      {field('plate', t('targets.customizationForm.plate_number'), 16)}
      {field('region', t('targets.customizationForm.plate_region'), 32)}
      <p className="text-hud-sub text-muted-foreground">{t('targets.customizationForm.the_plate_appears_on_the_mesh_front')}</p>
    </> : null}
    {field('role', t('targets.customizationForm.role'), 48)}
    <Field>
      <FieldLabel htmlFor={`${prefix}-description`}>{t('targets.customizationForm.identity_notes')}</FieldLabel>
      <Textarea id={`${prefix}-description`} rows={2} value={value.description} maxLength={256} disabled={!operator || busy} onChange={(e) => edit('description', e.target.value)} />
      <p className="text-hud-sub text-muted-foreground">{t('targets.customizationForm.notes_are_text_only_they_do_not')}</p>
    </Field>
    {target.life || target.activity ? <p className="text-hud-sub text-muted-foreground">{t('targets.customizationForm.scene_controlled_actor_replacement_must_fit_the')}</p> : null}
    {errors.length && (dirty || creating) ? <p role="alert" className="text-hud-sub">{t('targets.customizationForm.enter_a_target_name_and_check_text')}</p> : null}
    {operator ? <div className="flex flex-wrap gap-2">
      <Button size="sm" disabled={busy || errors.length > 0 || (!creating && !dirty)} onClick={() => void submit()}>{busy ? (t('targets.customizationForm.saving')) : creating ? (t('targets.customizationForm.next_place_in_scene')) : (t('targets.customizationForm.save_appearance_and_identity'))}</Button>
      {!creating ? <Button size="sm" variant="outline" disabled={busy || !dirty} onClick={() => { setValue(baseline); setSaved(false) }}>{t('targets.customizationForm.discard_changes')}</Button> : null}
    </div> : null}
    {saved ? <p role="status">{t('targets.customizationForm.configuration_saved')}</p> : null}
  </div>
}
