import * as React from 'react'
import type { Target } from '@awr/contracts/types'
import { getLocale, useT } from '@/app/i18n'
import { APPEARANCE_ENABLED, assetFor, assetStatus, subscribeAssets, retryAsset } from '@/app/shell/engineD2'
import { targetsModel } from '@/app/shell/engineD2'
import { camera } from '@/viewport/facade'
import { prefs } from '@/stores/prefs'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldLabel } from '@/ui/components/ui/field'

export function AppearanceEditor({ id, target }: { id: string; target: Target }) {
  const selected = target.appearance?.asset_id ?? ''
  const load = React.useSyncExternalStore(subscribeAssets, () => assetStatus(selected))
  const t = useT()
  if (!APPEARANCE_ENABLED) return null
  const zh = getLocale() === 'zh-CN'
  const current = assetFor(selected)
  const message = !selected ? (t('targets.appearanceEditor.no_mesh_selected')) : {
    idle: t('targets.appearanceEditor.queued_open_the_viewport_to_load'),
    loading: t('targets.appearanceEditor.loading_mesh_and_texture'),
    ready: t('targets.appearanceEditor.mesh_ready_bound_to_simulation_pose'),
    error: t('targets.appearanceEditor.asset_failed_showing_wireframe_you_can_retry'),
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
    <FieldLabel>{t('targets.appearanceEditor.applied_model_and_inspection')}</FieldLabel>
    <p className="text-hud-sub">{current ? (zh ? current.name_zh : current.name_en) : (t('targets.appearanceEditor.original_wireframe'))}</p>
    <p className="text-hud-sub text-muted-foreground" role="status">{message}</p>
    {load === 'error' ? <Button size="sm" variant="outline" onClick={() => retryAsset(selected)}>{t('targets.appearanceEditor.retry_model_download')}</Button> : null}
    {current ? <p className="text-hud-sub text-muted-foreground">{current.license} · {current.triangles.toLocaleString()} {t('targets.appearanceEditor.triangles')}</p> : null}
    <p className="text-hud-sub text-muted-foreground">{t('targets.appearanceEditor.persistent_identity', { value1: id })}</p>
    <Button size="sm" variant="outline" onClick={() => inspect()}>{t('targets.appearanceEditor.inspect_and_follow_target')}</Button>
    {target.class === 'vehicle' && target.metadata?.plate_text ? <Button size="sm" variant="outline" onClick={() => inspect(true)}>{t('targets.appearanceEditor.inspect_front_plate')}</Button> : null}
    <p className="text-hud-sub text-muted-foreground">{t('targets.appearanceEditor.overview_only_drag_or_change_camera_view')}</p>
  </Field>
}
