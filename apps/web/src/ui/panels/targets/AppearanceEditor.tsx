import * as React from 'react'
import type { Target } from '@awr/contracts/types'
import { getLocale } from '@/app/i18n'
import { APPEARANCE_ENABLED, assetFor, assetStatus, subscribeAssets, retryAsset } from '@/app/shell/engineD2'
import { targetsModel } from '@/app/shell/engineD2'
import { camera } from '@/viewport/facade'
import { prefs } from '@/stores/prefs'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldLabel } from '@/ui/components/ui/field'

export function AppearanceEditor({ id, target }: { id: string; target: Target }) {
  const selected = target.appearance?.asset_id ?? ''
  const load = React.useSyncExternalStore(subscribeAssets, () => assetStatus(selected))
  if (!APPEARANCE_ENABLED) return null
  const zh = getLocale() === 'zh-CN'
  const current = assetFor(selected)
  const message = !selected ? (zh ? '尚未选择精细模型' : 'No mesh selected') : {
    idle: zh ? '等待加载；打开视口后继续' : 'Queued; open the viewport to load',
    loading: zh ? '正在加载网格与贴图…' : 'Loading mesh and texture…',
    ready: zh ? '模型已加载 · 跟随仿真位置与朝向' : 'Mesh ready · bound to simulation pose',
    error: zh ? '模型加载失败，暂显示线框；可重试加载' : 'Asset failed; showing wireframe. You can retry loading.',
  }[load]
  const inspect = (plate = false) => {
    const row = targetsModel.rowOf(id)
    if (row < 0) return
    prefs.setUi('mobileTab', 'viewport')
    const r = targetsModel.rows
    camera.inspectTarget(Array.from(r.pos.subarray(row * 3, row * 3 + 3)), targetsModel.sizeOf(id, targetsModel.classOfRow(row)), r.heading[row], (out) => {
      const currentRow = targetsModel.rowOf(id)
      if (currentRow < 0 || r.hidden[currentRow] || !r.enabled[currentRow]) return false
      const size = targetsModel.sizeOf(id, targetsModel.classOfRow(currentRow))
      const front = plate ? size[0] / 2 + 0.015 : 0
      out[0] = r.pos[currentRow * 3] + Math.cos(r.heading[currentRow]) * front
      out[1] = r.pos[currentRow * 3 + 1] + Math.sin(r.heading[currentRow]) * front
      out[2] = r.pos[currentRow * 3 + 2] + (plate ? Math.min(0.65, size[2] * 0.3) : size[2] * 0.5)
      return true
    }, plate)
  }
  return <Field className="col-span-2" data-target-appearance={selected || 'wireframe'}>
    <FieldLabel>{zh ? '已应用的模型与观察' : 'Applied model and inspection'}</FieldLabel>
    <p className="text-hud-sub">{current ? (zh ? current.name_zh : current.name_en) : (zh ? '原生线框' : 'Original wireframe')}</p>
    <p className="text-hud-sub text-muted-foreground" role="status">{message}</p>
    {load === 'error' ? <Button size="sm" variant="outline" onClick={() => retryAsset(selected)}>{zh ? '重试加载模型' : 'Retry model download'}</Button> : null}
    {current ? <p className="text-hud-sub text-muted-foreground">{current.license} · {current.triangles.toLocaleString()} {zh ? '三角面' : 'triangles'}</p> : null}
    <p className="text-hud-sub text-muted-foreground">{zh ? `持续身份：${id}` : `Persistent identity: ${id}`}</p>
    <Button size="sm" variant="outline" onClick={() => inspect()}>{zh ? '近看并跟随目标' : 'Inspect and follow target'}</Button>
    {target.class === 'vehicle' && target.metadata?.plate_text ? <Button size="sm" variant="outline" onClick={() => inspect(true)}>{zh ? '近看前车牌' : 'Inspect front plate'}</Button> : null}
    <p className="text-hud-sub text-muted-foreground">{zh ? '仅移动观察视角；拖动视口或切换视角退出跟随。目标进入设施后停止跟随。' : 'Overview only. Drag or change camera view to stop; following also stops at facility entry.'}</p>
  </Field>
}
