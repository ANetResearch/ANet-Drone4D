// Loading layer of a world switch and of static browsing (AWR-04 §10.1 last rule; M15-FR-201, FR-210; UX-FR-102;
// AWR-14 §20.3.4): the compact D1 loading card (M15-FR-005) driven by the same five-segment model as the boot mask
// without the bundle segment (world 0.118, points 0.471, warming 0.353, reveal 0.059 after renormalising). Shaders are
// warm after the reveal, so the warming segment is complete at once; the opened-world snapshot of M05 and the direct
// M05 reports move world and points; reaching streaming completes the reveal segment. While a sandbox restarts in a new
// world the card says "starting the sandbox in the new city" (boot.phase.sandbox, no weight) until the session is ready.
// v0.3.0: the bar is the single bar of the boot mask (one fill, monotone total of the four segments) with the same
// readout (app/boot/loadMeter.ts): stage and percent above, first-screen bytes, throughput and remaining time below.
import * as React from 'react'
import { t as tr, useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { boot } from '@/app/boot/BootController'
import { ProgressWriter } from '@/app/boot/progress'
import { worldFloors } from '@/app/boot/progressSources'
import { LoadReadout, pageMeter, readoutTexts } from '@/app/boot/loadMeter'
import { BootProgressModel, SWITCH_SEGS, applyReport, onBootProgress } from '@/lib/bootProgress'
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

/** the bar, the phase line and the readout of one loading card (one model per card lifetime) */
function LoadingBar({ sandboxStarting }: { sandboxStarting: boolean }) {
  const t = useT()
  const bar = React.useRef<HTMLDivElement>(null)
  const fill = React.useRef<HTMLElement>(null)
  const phase = React.useRef<HTMLSpanElement>(null)
  const percent = React.useRef<HTMLSpanElement>(null)
  const bytes = React.useRef<HTMLSpanElement>(null)
  const rate = React.useRef<HTMLSpanElement>(null)
  React.useEffect(() => {
    const w = new ProgressWriter(fill.current, phase.current, new BootProgressModel(SWITCH_SEGS))
    const readout = new LoadReadout({ bar: bar.current, fill: fill.current, percent: percent.current, bytes: bytes.current, rate: rate.current }, pageMeter())
    readout.withCode = false
    readout.setTexts(readoutTexts(tr))
    readout.start()
    w.readout = readout
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
      readout.stop()
    }
  }, [])
  return (
    <>
      <div className="flex items-baseline justify-between gap-3 text-hud-sub tabular-nums">
        <span ref={phase} className="min-w-0 text-muted-foreground" data-loading-phase="" />
        <span ref={percent} className="shrink-0" aria-hidden="true" data-loading-percent="" />
      </div>
      <div ref={bar} className="loading-progress" data-loading-progress="" role="progressbar" aria-valuemin={0} aria-valuemax={100}
        aria-label={t('boot.progress.label')}>
        <i ref={fill} />
      </div>
      <div className="flex min-h-4 justify-between gap-3 whitespace-nowrap text-hud-cap text-muted-foreground tabular-nums" aria-hidden="true">
        <span ref={bytes} data-loading-bytes="" />
        <span ref={rate} data-loading-rate="" />
      </div>
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
