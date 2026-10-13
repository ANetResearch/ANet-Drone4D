// Loading layer of a world switch and of static browsing (AWR-04 §10.1 last rule; M15-FR-201, FR-210; UX-FR-102;
// AWR-14 §20.3.4): the compact D1 loading card (M15-FR-005) driven by the same five-segment model as the boot mask
// without the bundle segment (world 0.118, points 0.471, warming 0.353, reveal 0.059 after renormalising). Shaders are
// warm after the reveal, so the warming segment is complete at once; the opened-world snapshot of M05 and the direct
// M05 reports move world and points; reaching streaming completes the reveal segment. While a sandbox restarts in a new
// world the card says "starting the sandbox in the new city" (boot.phase.sandbox, no weight) until the session is ready.
// D2.1 (ADR-119; M15-FR-224): the bar is the segmented track of the boot mask (four segments here, widths by weight).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { boot } from '@/app/boot/BootController'
import { ProgressWriter } from '@/app/boot/progress'
import { worldFloors } from '@/app/boot/progressSources'
import { BOOT_WEIGHTS, BootProgressModel, SWITCH_SEGS, applyReport, onBootProgress } from '@/lib/bootProgress'
import { Button } from '@/ui/components/ui/button'
import { Card } from '@/ui/components/ui/card'
import { useSandbox } from '@/ui/sandbox/session'
import { useWorld, worldStore } from '@/stores/world'
import { power } from '@/app/power/PowerController'

function useRevealed(): boolean {
  const [revealed, setRevealed] = React.useState(boot.state === 'REVEALED')
  React.useEffect(() => boot.subscribe((s) => setRevealed(s === 'REVEALED')), [])
  return revealed
}

/** the bar and the phase line of one loading card (one model per card lifetime) */
function LoadingBar({ sandboxStarting }: { sandboxStarting: boolean }) {
  const t = useT()
  const bar = React.useRef<HTMLElement>(null)
  const phase = React.useRef<HTMLSpanElement>(null)
  const writer = React.useRef<ProgressWriter | null>(null)
  React.useEffect(() => {
    const w = new ProgressWriter(bar.current, phase.current, new BootProgressModel(SWITCH_SEGS))
    writer.current = w
    w.model.reportWarm('shaders', 1)
    w.model.reportWarm('ui', 1)
    const seed = worldFloors(worldStore.getState())
    w.model.report('world', seed.world)
    w.model.report('points', seed.points)
    if (seed.bytes) w.model.bytes = seed.bytes
    const off = onBootProgress((r) => {
      if (applyReport(w.model, r)) w.schedule()
    })
    const offWorld = worldStore.subscribe((s) => {
      const f = worldFloors(s)
      const gw = w.model.report('world', f.world)
      const gp = w.model.report('points', f.points)
      if (f.bytes) w.model.bytes = f.bytes
      const gr = s.phase === 'streaming' ? w.model.report('reveal', 1) : false
      const grew = gw || gp || gr
      if (grew) w.schedule()
    })
    w.schedule()
    return () => {
      off()
      offWorld()
      w.freeze()
      writer.current = null
    }
  }, [])
  return (
    <>
      <div className="loading-progress" data-loading-progress="">
        <i ref={bar}>
          {SWITCH_SEGS.map((k) => <i key={k} data-seg={k} style={{ flexGrow: Math.round(BOOT_WEIGHTS[k] * 100) }} />)}
        </i>
      </div>
      <span ref={phase} className="text-hud-sub text-muted-foreground" data-loading-phase="" />
      {sandboxStarting ? <span className="text-hud-sub text-muted-foreground" data-loading-sandbox="">{t('boot.phase.sandbox')}</span> : null}
    </>
  )
}

export function LoadingLayer() {
  const t = useT()
  const revealed = useRevealed()
  const phase = useWorld((s) => s.phase)
  const error = useWorld((s) => s.error)
  const worldId = useWorld((s) => s.worldId)
  const switching = useSandbox((s) => s.switching)
  const loading = phase === 'manifest' || phase === 'first_screen'
  if (!revealed || (!loading && phase !== 'error' && !switching)) return null
  return (
    <div data-anchor="center" data-world-loading={phase}>
      <Card size="sm" className="w-72 gap-2 rounded-xl bg-hud px-3 py-2">
        <span className="text-hud-title font-semibold">{switching?.world ?? worldId ?? ''}</span>
        {phase === 'error' && !switching ? (
          <>
            <span className="text-hud-sub text-muted-foreground">{error?.message ? `${error.code} ${error.message}` : t('world.phase.error')}</span>
            <div className="flex justify-end gap-2">
              <Button size="xs" variant="outline" onClick={() => navigate('/worlds')}>{t('world.backToList')}</Button>
              <Button size="xs" onClick={() => { power.expectNavigation(); location.reload() }}>{t('common.retry')}</Button>
            </div>
          </>
        ) : (
          <LoadingBar sandboxStarting={switching !== null && !loading} />
        )}
      </Card>
    </div>
  )
}
