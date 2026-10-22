import * as React from 'react'
import type { Target } from '@awr/contracts/types'
import { getLocale } from '@/app/i18n'
import { Button } from '@/ui/components/ui/button'
import { Input } from '@/ui/components/ui/input'
import { Textarea } from '@/ui/components/ui/textarea'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { customizationAssets, customizationErrors, customizationOf, customizationPatch, type Customization } from './customization'

/** Explicit save: switching models and entering a plate are submitted together, never on blur. */
export function CustomizationForm({ target, cls, operator, creating = false, save }: {
  target: Partial<Target>; cls: Target['class']; operator: boolean; creating?: boolean
  save: (patch: Partial<Target>) => Promise<boolean>
}) {
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
  const title = cls === 'person' ? (zh ? '人物外观' : 'Person appearance') : cls === 'vehicle' ? (zh ? '车型与外观' : 'Vehicle model') : cls === 'dog' ? (zh ? '犬只外观' : 'Dog appearance') : (zh ? '目标外观' : 'Appearance')
  const items = [{ value: '', label: zh ? '原生线框' : 'Original wireframe' }, ...entries.map((a) => ({ value: a.id, label: zh ? a.name_zh : a.name_en }))]
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
    {field('label', zh ? '目标名称' : 'Target name', 32)}
    <Field>
      <FieldLabel>{title}</FieldLabel>
      <Select items={items} value={value.assetId} disabled={!operator || busy} onValueChange={(v) => typeof v === 'string' && edit('assetId', v)}>
        <SelectTrigger className="w-full" aria-label={title}><SelectValue /></SelectTrigger>
        <SelectContent>{items.map((a) => <SelectItem key={a.value} value={a.value}>{a.label}</SelectItem>)}</SelectContent>
      </Select>
      <p className="text-hud-sub text-muted-foreground">{zh ? `可选 ${entries.length} 套精细模型；选择后使用该模型的真实外观。` : `${entries.length} mesh presets; selection uses the model's actual appearance.`}</p>
      {entries.length === 1 ? <p className="text-hud-sub text-muted-foreground">{zh ? '当前仅有这一套精细资产，尚不支持独立修改品种、毛色或部件。' : 'One mesh is available; separate breed, coat and part editing is not supported.'}</p> : null}
      {asset ? <p className="text-hud-sub text-muted-foreground">{asset.dimensions_m.map((n) => n.toFixed(2)).join(' × ')} m · {asset.license}</p> : null}
    </Field>
    {cls === 'vehicle' ? <>
      {field('plate', zh ? '车牌号码' : 'Plate number', 16)}
      {field('region', zh ? '车牌所属地区' : 'Plate region', 32)}
      <p className="text-hud-sub text-muted-foreground">{zh ? '车牌会显示在精细车辆的前后牌照上。留空可移除文字；字母自动转为大写。' : 'The plate appears on the mesh front and rear. Empty removes the text; letters are uppercased.'}</p>
    </> : null}
    {field('role', zh ? '角色说明' : 'Role', 48)}
    <Field>
      <FieldLabel htmlFor={`${prefix}-description`}>{zh ? '身份备注' : 'Identity notes'}</FieldLabel>
      <Textarea id={`${prefix}-description`} rows={2} value={value.description} maxLength={256} disabled={!operator || busy} onChange={(e) => edit('description', e.target.value)} />
      <p className="text-hud-sub text-muted-foreground">{zh ? '备注只保存文字，不会自动改变衣着、车身颜色或行为。' : 'Notes are text only; they do not alter clothing, paint or behavior.'}</p>
    </Field>
    {target.life || target.activity ? <p className="text-hud-sub text-muted-foreground">{zh ? '当前目标由场景活动驱动。外观替换需通过路线尺寸检查；保存不改变身份或活动进度。' : 'Scene-controlled actor. Replacement must fit the admitted route; saving preserves identity and activity progress.'}</p> : null}
    {errors.length && (dirty || creating) ? <p role="alert" className="text-hud-sub">{zh ? '请填写目标名称，并检查车牌格式和文本长度。车牌可含大写字母、数字、汉字、空格及连字符。' : 'Enter a target name and check text lengths and plate characters (letters, digits, Chinese characters, spaces and hyphens).'}</p> : null}
    {operator ? <div className="flex flex-wrap gap-2">
      <Button size="sm" disabled={busy || errors.length > 0 || (!creating && !dirty)} onClick={() => void submit()}>{busy ? (zh ? '保存中…' : 'Saving…') : creating ? (zh ? '下一步：在场景中放置' : 'Next: place in scene') : (zh ? '保存外观与身份' : 'Save appearance and identity')}</Button>
      {!creating ? <Button size="sm" variant="outline" disabled={busy || !dirty} onClick={() => { setValue(baseline); setSaved(false) }}>{zh ? '撤销未保存修改' : 'Discard changes'}</Button> : null}
    </div> : null}
    {saved ? <p role="status">{zh ? '配置已保存' : 'Configuration saved'}</p> : null}
  </div>
}
