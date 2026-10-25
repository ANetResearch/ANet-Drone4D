import * as React from 'react'
import { useT } from '@/app/i18n'
import { APPEARANCE_ENABLED, AppearancePreview, assetFor, type PreviewView } from '@/app/shell/engineD2'
import { Button } from '@/ui/components/ui/button'
import type { Target } from '@awr/contracts/types'
import type { LifeSample } from '@/ui/sandbox/domain'
import { BehaviorPreview } from './BehaviorPreview'

/** Draft-only component: plate keystrokes update the viewer, never save target configuration. */
export function AppearancePreviewCard({ assetId, plate, life }: { assetId: string; plate: string; life?: Target['life'] }) {
  const t = useT()
  const canvas = React.useRef<HTMLCanvasElement>(null)
  const viewer = React.useRef<AppearancePreview | null>(null)
  const [status, setStatus] = React.useState<'loading' | 'ready' | 'error'>('loading')
  const [attempt, setAttempt] = React.useState(0)
  const asset = assetFor(assetId)
  const [frame, setFrame] = React.useState<LifeSample | null>(null)
  const applyFrame = React.useCallback((value: LifeSample | null) => { setFrame(value) }, [])
  React.useEffect(() => {
    if (!canvas.current || !asset || !APPEARANCE_ENABLED) return
    let active = true
    try { viewer.current = new AppearancePreview(canvas.current, asset, setStatus) }
    catch { queueMicrotask(() => { if (active) setStatus('error') }) }
    return () => { active = false; viewer.current?.dispose(); viewer.current = null }
  }, [asset, attempt])
  React.useEffect(() => { viewer.current?.setPlate(plate) }, [plate, asset, attempt])
  React.useEffect(() => { viewer.current?.setMotion(frame?.distance ?? 0, frame?.speed ?? 0, frame?.hidden ?? false) }, [frame, status])
  if (!APPEARANCE_ENABLED) return null
  const views: PreviewView[] = ['left', 'right', 'in', 'out', 'front', 'back', 'reset']
  return <section className="min-w-0 rounded-md border p-2" aria-label={t('targets.preview.title')} data-appearance-preview={assetId}>
    <p className="mb-2 text-hud-sub font-medium">{t('targets.preview.title')}</p>
    {asset ? <>
      <div className="relative overflow-hidden rounded-md">
        <canvas ref={canvas} className="block h-52 w-full touch-none cursor-grab" aria-label={t('targets.preview.canvas')} />
        {frame?.hidden && status === 'ready' ? <p className="absolute inset-0 flex items-center justify-center p-4 text-center text-hud-sub">{t('targets.activityCard.inside_the_registered_facility_identity_retained')}</p> : null}
        {status !== 'ready' ? <div className="absolute inset-0 flex items-center justify-center bg-background/90 p-3 text-center text-hud-sub" role="status">
          {t(status === 'loading' ? 'targets.preview.loading' : 'targets.preview.error')}
        </div> : null}
      </div>
      {status === 'error' ? <Button size="sm" variant="outline" onClick={() => { setStatus('loading'); setAttempt((v) => v + 1) }}>{t('targets.preview.retry')}</Button> : null}
      <div className="mt-2 flex flex-wrap gap-1">
        {views.map((view) => <Button key={view} size="sm" variant="outline" disabled={status !== 'ready'} onClick={() => viewer.current?.view(view)}>{t(`targets.preview.${view}`)}</Button>)}
        {asset.class === 'vehicle' ? <Button size="sm" variant="outline" disabled={status !== 'ready'} onClick={() => viewer.current?.view('plate')}>{t('targets.preview.plate')}</Button> : null}
      </div>
      <p className="mt-2 text-hud-sub text-muted-foreground">{t('targets.preview.hint')}</p>
    </> : <p className="text-hud-sub text-muted-foreground">{t('targets.preview.wireframe')}</p>}
    {life ? <BehaviorPreview key={`${life.sha256}:${life.actor_id}`} sha={life.sha256} actorId={life.actor_id} onFrame={applyFrame} /> : <p className="mt-2 text-hud-sub text-muted-foreground">{t('targets.rehearsal.noRecipe')}</p>}
  </section>
}
