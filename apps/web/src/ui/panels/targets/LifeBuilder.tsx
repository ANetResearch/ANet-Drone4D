import * as React from 'react'
import { getLocale } from '@/app/i18n'
import { APPEARANCE_ENABLED } from '@/app/shell/engineD2'
import { sbSession, type LifePreview } from '@/ui/sandbox/domain'
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
export function LifeBuilder({ operator }: { operator: boolean }) {
  const zh = getLocale() === 'zh-CN'
  const [seed, setSeed] = React.useState(20261022)
  const [people, setPeople] = React.useState(12)
  const [vehicles, setVehicles] = React.useState(4)
  const [extras, setExtras] = React.useState<Record<string, number>>({ dog: 0, uav: 0, machine: 0, object: 0 })
  const [vehicleTypes, setVehicleTypes] = React.useState(['sedan', 'suv', 'hatchback'])
  const [prompt, setPrompt] = React.useState('')
  const [preview, setPreview] = React.useState<LifePreview | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [message, setMessage] = React.useState('')
  if (!operator || !APPEARANCE_ENABLED) return null
  const generate = async (mode: 'seed' | 'deepseek') => {
    setBusy(true); setMessage(''); setPreview(null)
    try { setPreview(await sbSession.lifePreview({ seed, people, vehicles, extras, vehicle_types: vehicleTypes, mode, prompt })) }
    catch (e) { setMessage(errorMessage(e)) }
    finally { setBusy(false) }
  }
  const apply = async () => {
    if (!preview?.audit.admitted || !preview.sha256) return
    setBusy(true); setMessage('')
    try {
      const snapshot = await sandbox.exportSnapshot()
      if (!snapshot) throw new Error(zh ? '无法保存当前场景' : 'Could not save current scene')
      const error = await sandbox.restore({ ...snapshot, targets: preview.targets, seed: preview.seed })
      if (error) throw new Error(error.message)
      setMessage(zh ? '新目标系统已应用，仿真从 0 秒开始' : 'Population applied; simulation restarts at zero')
      setPreview(null)
    } catch (e) { setMessage(e instanceof Error ? e.message : 'Apply failed') }
    finally { setBusy(false) }
  }
  const counts = [people, vehicles, ...Object.values(extras)]
  const total = counts.reduce((a, b) => a + b, 0)
  const countsOK = counts.every((n) => Number.isInteger(n) && n >= 0) && total >= 1 && total <= 30 && (vehicles === 0 || vehicleTypes.length > 0)
  const actors = Object.values(preview?.audit.actors ?? {})
  const mobileActors = actors.filter((a) => !a.stationary)
  return <details className="rounded-md border p-2 text-xs" data-life-builder="">
    <summary className="cursor-pointer font-medium">{zh ? '场景生活生成器' : 'Scene life generator'}</summary>
    <div className="mt-2 flex flex-col gap-2">
      <p className="text-muted-foreground">{zh ? '同一场景、引擎版本、配置与 seed 可重放。生成时当前世界继续运行；应用后从头开始。' : 'Scene, engine, configuration and seed define a replay. Preview leaves the current world running; apply resets it.'}</p>
      <label>{zh ? '随机种子' : 'Seed'}<Input disabled={busy} type="number" value={seed} min={0} max={4294967295} onChange={(e) => { setSeed(Number(e.target.value)); setPreview(null) }} /></label>
      <div className="grid grid-cols-2 gap-2">
        <label>{zh ? '行人数' : 'People'}<Input disabled={busy} type="number" min={0} max={30} value={people} onChange={(e) => { setPeople(Number(e.target.value)); setPreview(null) }} /></label>
        <label>{zh ? '车辆数' : 'Vehicles'}<Input disabled={busy} type="number" min={0} max={30} value={vehicles} onChange={(e) => { setVehicles(Number(e.target.value)); setPreview(null) }} /></label>
      </div>
      <div className="grid grid-cols-2 gap-2">
        {(['dog','machine','uav','object'] as const).map((cls,i) => <label key={cls}>{zh ? ['社区犬','后勤叉车','巡检无人机','公共设施'][i] : cls}<Input disabled={busy} type="number" min={0} max={30} value={extras[cls]} onChange={(e) => { setExtras({ ...extras, [cls]: Number(e.target.value) }); setPreview(null) }} /></label>)}
      </div>
      <fieldset className="grid grid-cols-2 gap-1"><legend>{zh ? '车辆类型' : 'Vehicle types'}</legend>
        {['sedan','suv','hatchback','van','pickup','bus','truck'].map((kind,i) => <label key={kind} className="flex items-center gap-1"><Checkbox disabled={busy} checked={vehicleTypes.includes(kind)} onCheckedChange={(checked) => { setVehicleTypes(checked ? [...vehicleTypes,kind] : vehicleTypes.filter((v) => v !== kind)); setPreview(null) }} />{zh ? ['轿车','SUV','掀背车','厢式货车','皮卡','公交车','卡车'][i] : kind}</label>)}
      </fieldset>
      <p className="text-muted-foreground">{zh ? `共 ${total} / 30 个目标。车型、作业区与航路必须通过场景准入；大型车辆可能因转弯或停靠空间不足被拒绝。公共设施按真实用途保持固定。` : `${total} / 30 actors. Classes and routes require scene admission; large vehicles may lack suitable clearance. Installed objects remain fixed.`}</p>
      <Button variant="outline" size="sm" disabled={busy || !countsOK} onClick={() => void generate('seed')}>{busy ? (zh ? '生成与整段验收中…' : 'Generating and auditing…') : (zh ? '生成固定 seed 预览' : 'Preview seeded population')}</Button>
      <label>{zh ? '定制行为要求（可选）' : 'Custom activity request (optional)'}<Textarea disabled={busy} value={prompt} maxLength={2000} rows={3} onChange={(e) => { setPrompt(e.target.value); setPreview(null) }} placeholder={zh ? '例如：让两位居民先去不同商店，再回各自住宅；其余居民照常生活。' : 'Describe purposeful activity changes.'} /></label>
      <Button variant="outline" size="sm" disabled={busy || !countsOK || !prompt.trim()} onClick={() => void generate('deepseek')}>{zh ? '使用 DeepSeek Pro 生成（计费）' : 'Generate with DeepSeek Pro (paid)'}</Button>
      {preview ? <div className="flex flex-col gap-1" role="status">
        <strong>{preview.audit.admitted ? (zh ? '25 分钟准入检查通过' : '25-minute admission passed') : (zh ? '未通过准入，不应用此方案' : 'Admission failed; cannot apply')}</strong>
        <span>{zh ? `全程参与更新：${actors.filter((a) => a.updates === preview.audit.ticks).length} / ${actors.length} 个目标，每个 ${preview.audit.ticks} 步` : `${actors.filter((a) => a.updates === preview.audit.ticks).length} / ${actors.length} actors updated every step`}</span>
        {actors.length ? <span>{zh ? `${mobileActors.length} 个活动目标 · ${actors.length - mobileActors.length} 个固定设施` : `${mobileActors.length} mobile actors · ${actors.length - mobileActors.length} installed objects`}</span> : null}
        {mobileActors.length ? <span>{zh ? `活动目标最少移动 ${Math.round(Math.min(...mobileActors.map((a) => a.distance_m)))} m · 最长行程等待 ${Math.round(Math.max(...mobileActors.map((a) => a.max_no_progress_s)))} s` : `Minimum travel ${Math.round(Math.min(...mobileActors.map((a) => a.distance_m)))} m`}</span> : null}
        {preview.design?.summary ? <p>{preview.design.summary}</p> : null}
        {preview.provider_usage ? <span>{preview.provider_usage.requested_model} · {preview.provider_usage.usage?.total_tokens ?? '?'} tokens</span> : null}
        {preview.audit.failures.slice(0,5).map((f,i) => <span key={i}>{f.actor}: {f.why} {f.seconds ?? ''}</span>)}
        <Button size="sm" disabled={busy || !preview.audit.admitted} onClick={() => void apply()}>{zh ? '应用并从 0 秒开始' : 'Apply and restart from zero'}</Button>
      </div> : null}
      {message ? <p role="status">{message}</p> : null}
    </div>
  </details>
}
