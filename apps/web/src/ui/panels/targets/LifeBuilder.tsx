import * as React from 'react'
import { useT } from '@/app/i18n'
import { APPEARANCE_ENABLED } from '@/app/shell/engineD2'
import { lifeApi, type LifePreview } from '@/ui/sandbox/lifeApi'
import { sandbox } from '@/ui/sandbox/session'
import { Button } from '@/ui/components/ui/button'
import { Input } from '@/ui/components/ui/input'
import { Checkbox } from '@/ui/components/ui/checkbox'
import { Textarea } from '@/ui/components/ui/textarea'
import { ApiError } from '@/net/api'

function errorMessage(e: unknown): string {
  if (e instanceof ApiError && e.detail && typeof e.detail === 'object' && 'reason' in e.detail && typeof e.detail.reason === 'string') return e.detail.reason
  return e instanceof Error ? e.message : 'Generation failed'
}

/** Preview/admission does not alter the live world. Applying is an explicit reset. */
export interface LifeBuilderInitial { seed: number; people: number; vehicles: number; extras: Record<string, number>; vehicleTypes: string[] }
export function LifeBuilder({ operator, initial }: { operator: boolean; initial?: LifeBuilderInitial }) {
  const t = useT()
  const [seed, setSeed] = React.useState(initial?.seed ?? 20261022)
  const [people, setPeople] = React.useState(initial?.people ?? 12)
  const [vehicles, setVehicles] = React.useState(initial?.vehicles ?? 4)
  const [extras, setExtras] = React.useState<Record<string, number>>(initial?.extras ?? { dog: 0, uav: 0, machine: 0, object: 0 })
  const [vehicleTypes, setVehicleTypes] = React.useState(initial?.vehicleTypes ?? ['sedan', 'suv', 'hatchback'])
  const [prompt, setPrompt] = React.useState('')
  const [preview, setPreview] = React.useState<LifePreview | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [message, setMessage] = React.useState('')
  if (!operator || !APPEARANCE_ENABLED) return null
  const generate = async (mode: 'seed' | 'deepseek') => {
    setBusy(true); setMessage(''); setPreview(null)
    try { setPreview(await lifeApi.preview({ seed, people, vehicles, extras, vehicle_types: vehicleTypes, mode, prompt })) }
    catch (e) { setMessage(errorMessage(e)) }
    finally { setBusy(false) }
  }
  const apply = async () => {
    if (!preview?.audit.admitted || !preview.sha256) return
    setBusy(true); setMessage('')
    try {
      const snapshot = await sandbox.exportSnapshot()
      if (!snapshot) throw new Error(t('targets.lifeBuilder.could_not_save_current_scene'))
      const error = await sandbox.restore({ ...snapshot, targets: preview.targets, seed: preview.seed })
      if (error) throw new Error(error.message)
      setMessage(t('targets.lifeBuilder.population_applied_simulation_restarts_at_zero'))
      setPreview(null)
    } catch (e) { setMessage(e instanceof Error ? e.message : 'Apply failed') }
    finally { setBusy(false) }
  }
  const counts = [people, vehicles, ...Object.values(extras)]
  const total = counts.reduce((a, b) => a + b, 0)
  const countsOK = counts.every((n) => Number.isInteger(n) && n >= 0) && total >= 1 && total <= 30 && (vehicles === 0 || vehicleTypes.length > 0)
  const actors = Object.values(preview?.audit.actors ?? {})
  const mobileActors = actors.filter((a) => !a.stationary)
  return <details open={initial ? true : undefined} className="rounded-md border p-2 text-xs" data-life-builder="">
    <summary className="cursor-pointer font-medium">{t('targets.lifeBuilder.scene_life_generator')}</summary>
    <div className="mt-2 flex flex-col gap-2">
      <p className="text-muted-foreground">{t('targets.lifeBuilder.scene_engine_configuration_and_seed_define_a')}</p>
      {initial ? <p>{t('targets.population.replaceWarning')}</p> : null}
      <label>{t('targets.lifeBuilder.seed')}<Input disabled={busy} type="number" value={seed} min={0} max={4294967295} onChange={(e) => { setSeed(Number(e.target.value)); setPreview(null) }} /></label>
      <div className="grid grid-cols-2 gap-2">
        <label>{t('targets.lifeBuilder.people')}<Input disabled={busy} type="number" min={0} max={30} value={people} onChange={(e) => { setPeople(Number(e.target.value)); setPreview(null) }} /></label>
        <label>{t('targets.lifeBuilder.vehicles')}<Input disabled={busy} type="number" min={0} max={30} value={vehicles} onChange={(e) => { setVehicles(Number(e.target.value)); setPreview(null) }} /></label>
      </div>
      <div className="grid grid-cols-2 gap-2">
        {(['dog','machine','uav','object'] as const).map((cls) => <label key={cls}>{t(`targets.lifeClass.${cls}`)}<Input disabled={busy} type="number" min={0} max={30} value={extras[cls]} onChange={(e) => { setExtras({ ...extras, [cls]: Number(e.target.value) }); setPreview(null) }} /></label>)}
      </div>
      <fieldset className="grid grid-cols-2 gap-1"><legend>{t('targets.lifeBuilder.vehicle_types')}</legend>
        {['sedan','suv','hatchback','van','pickup','bus','truck'].map((kind) => <label key={kind} className="flex items-center gap-1"><Checkbox disabled={busy} checked={vehicleTypes.includes(kind)} onCheckedChange={(checked) => { setVehicleTypes(checked ? [...vehicleTypes,kind] : vehicleTypes.filter((v) => v !== kind)); setPreview(null) }} />{t(`targets.lifeVehicle.${kind}`)}</label>)}
      </fieldset>
      <p className="text-muted-foreground">{t('targets.lifeBuilder.actors_classes_and_routes_require_scene_admission', { value1: total })}</p>
      <Button variant="outline" size="sm" disabled={busy || !countsOK} onClick={() => void generate('seed')}>{busy ? (t('targets.lifeBuilder.generating_and_auditing')) : (t('targets.lifeBuilder.preview_seeded_population'))}</Button>
      <label>{t('targets.lifeBuilder.custom_activity_request_optional')}<Textarea disabled={busy} value={prompt} maxLength={2000} rows={3} onChange={(e) => { setPrompt(e.target.value); setPreview(null) }} placeholder={t('targets.lifeBuilder.describe_purposeful_activity_changes')} /></label>
      <Button variant="outline" size="sm" disabled={busy || !countsOK || !prompt.trim()} onClick={() => void generate('deepseek')}>{t('targets.lifeBuilder.generate_with_deepseek_pro_paid')}</Button>
      {preview ? <div className="flex flex-col gap-1" role="status">
        <strong>{preview.audit.admitted ? (t('targets.lifeBuilder.minute_admission_passed')) : (t('targets.lifeBuilder.admission_failed_cannot_apply'))}</strong>
        <span>{t('targets.lifeBuilder.actors_updated_every_step', { value1: actors.filter((a) => a.updates === preview.audit.ticks).length, value2: actors.length, value3: preview.audit.ticks })}</span>
        {actors.length ? <span>{t('targets.lifeBuilder.mobile_actors_installed_objects', { value1: mobileActors.length, value2: actors.length - mobileActors.length })}</span> : null}
        {mobileActors.length ? <span>{t('targets.lifeBuilder.minimum_travel_m', { value1: Math.round(Math.min(...mobileActors.map((a) => a.distance_m))), value2: Math.round(Math.max(...mobileActors.map((a) => a.max_no_progress_s))) })}</span> : null}
        {preview.design?.summary ? <p>{preview.design.summary}</p> : null}
        {preview.provider_usage ? <span>{preview.provider_usage.requested_model} · {preview.provider_usage.usage?.total_tokens ?? '?'} tokens</span> : null}
        {preview.audit.failures.slice(0,5).map((f,i) => <span key={i}>{f.actor}: {f.why} {f.seconds ?? ''}</span>)}
        <Button size="sm" disabled={busy || !preview.audit.admitted} onClick={() => void apply()}>{t('targets.lifeBuilder.apply_and_restart_from_zero')}</Button>
      </div> : null}
      {message ? <p role="status">{message}</p> : null}
    </div>
  </details>
}
