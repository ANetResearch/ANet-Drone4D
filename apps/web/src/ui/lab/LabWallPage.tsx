// /lab/runs/:rid, multi-UAV observation wall (AWR-14 §21.7; R-D3-09, R-D3-11; P0a-1 replay of recorded runs). Each tile
// is one UAV's private view (agent_view); the evaluation view adds truth overlays and, for runs with an event, the event
// plan panel (tape decoded in the browser, P0a-2 event replay).
// Grid 1/4/9/16 (automatic = smallest that fits), sensor and RGB/depth selection, paging over 16 tiles, double click to
// zoom one tile. The lab playhead drives every tile; the footer timeline plays, pauses and seeks.
import { useQuery } from '@tanstack/react-query'
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate, type RouteProps } from '@/app/router/router'
import { benchGet, ROLE_RANK, type BenchRun } from '@/net/bench'
import { usePlayhead } from '@/stores/lab/playhead'
import { PanelEmpty } from '@/ui/brand'
import { Button } from '@/ui/components/ui/button'
import { Slider } from '@/ui/components/ui/slider'
import { toast } from '@/ui/components/ui/toast'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { useWindowSize } from '@/ui/layout/layoutState'
import { useElementWidth } from '@/ui/layout/useElementWidth'
import { EventPlan } from './wall/EventPlan'
import { GRIDS, layoutWall, type GridSize } from './wall/layout'
import { WallTile } from './wall/WallTile'
import { useBenchSession } from './useBenchSession'

const clock = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${(s % 60).toFixed(1).padStart(4, '0')}`

export function LabWallPage({ params, search }: RouteProps) {
  const t = useT()
  const rid = params.rid!
  const session = useBenchSession()
  const run = useQuery({ queryKey: ['lab', 'run', rid], queryFn: () => benchGet<BenchRun>(`/runs/${encodeURIComponent(rid)}`), enabled: session !== null })
  const [grid, setGrid] = React.useState<GridSize | 'auto'>(() => {
    const g = Number(search.get('grid'))
    return (GRIDS as readonly number[]).includes(g) ? (g as GridSize) : 'auto'
  })
  const [mode, setMode] = React.useState<'rgb' | 'depth'>('rgb')
  const canEval = session !== null && ROLE_RANK[session.role] >= ROLE_RANK.evaluator
  const [evalWanted, setEvalWanted] = React.useState(search.get('view') === 'eval')
  const evalView = evalWanted && canEval
  const denied = evalWanted && session !== null && !canEval
  React.useEffect(() => {
    // view=eval without the role shows the observation view with one notice (AWR-14 §21.2.3 item 2)
    if (denied) toast.add({ type: 'info', title: t('lab.wall.evalDenied') })
  }, [denied, t])
  const [page, setPage] = React.useState(0)
  const [sel, setSel] = React.useState<string | null>(null)
  const [zoom, setZoom] = React.useState<string | null>(null)
  const ph = usePlayhead()
  const [box, bw] = useElementWidth<HTMLDivElement>(960)
  const { h: wh } = useWindowSize()

  React.useEffect(() => {
    document.title = t('lab.wall.docTitle', { rid })
    usePlayhead.setState({ tS: Number(search.get('t')) || 0, playing: false, durationS: 0 })
  }, [rid]) // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {
    if (!ph.playing) return
    let last = performance.now()
    let raf = requestAnimationFrame(function step(now) {
      usePlayhead.getState().advance((now - last) / 1000)
      last = now
      raf = requestAnimationFrame(step)
    })
    return () => cancelAnimationFrame(raf)
  }, [ph.playing])

  const chans = (run.data?.frames ?? []).filter((c) => !c.sensor_id.includes('.'))
  const cams = [...new Set(chans.map((c) => c.sensor_id))].sort()
  const [cam, setCam] = React.useState<string | null>(null)
  const camSel = cam && cams.includes(cam) ? cam : (cams[0] ?? 'cam0')
  const hasDepth = (run.data?.frames ?? []).some((c) => c.sensor_id === `${camSel}.depth`)
  const uavs = chans.filter((c) => c.sensor_id === camSel).map((c) => c.uav_id).sort()
  const shown = zoom ? [zoom] : uavs
  const L = layoutWall(shown.length, bw, Math.max(240, wh - 190), zoom ? 1 : grid === 'auto' ? undefined : grid)
  const pg = Math.min(page, L.pages - 1)
  const tiles = shown.slice(pg * L.cap, (pg + 1) * L.cap)
  // the timeline spans the latest frame of any tile; before the first frame of a tile there is nothing to show, so an
  // idle playhead at the start snaps to it
  const onSpan = React.useCallback((first: number, last: number) => {
    const st = usePlayhead.getState()
    if (last > st.durationS) st.setDuration(last)
    if (!st.playing && st.tS < first) st.seek(first)
  }, [])

  if (run.isError) {
    return (
      <section className="app-layer-overlay-page bg-background p-4">
        <PanelEmpty title={t('lab.wall.unavailable')} description={t('lab.unreachableHint')}
          action={<Button size="sm" variant="outline" onClick={() => navigate('/lab/runs')}>{t('lab.wall.back')}</Button>} />
      </section>
    )
  }
  return (
    <section data-view="lab-wall" data-figure="lab-wall" className="app-layer-overlay-page flex flex-col bg-background">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <Button size="sm" variant="ghost" onClick={() => navigate('/lab/runs')}>
          <Icon icon="chev.left" data-icon="inline-start" />
          {t('lab.wall.back')}
        </Button>
        <h1 className="font-mono text-ed-title font-bold">{rid}</h1>
        <span className="text-hud-sub text-muted-foreground">{run.data?.world_id ?? ''}</span>
        {evalView ? (
          <span data-eval-tag className="rounded-sm border border-foreground px-1.5 text-hud-sub font-bold">{t('lab.wall.evalView')}</span>
        ) : null}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <span className="text-hud-sub text-muted-foreground">{t('lab.wall.grid')}</span>
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[String(grid)]} aria-label={t('lab.wall.grid')}
            onValueChange={(v: unknown[]) => {
              const g = v[0] === 'auto' ? 'auto' : Number(v[0])
              if (g === 'auto' || (GRIDS as readonly number[]).includes(g)) {
                setGrid(g as GridSize | 'auto')
                setPage(0)
              }
            }}>
            <ToggleGroupItem value="auto">{t('lab.wall.gridAuto')}</ToggleGroupItem>
            {GRIDS.map((g) => <ToggleGroupItem key={g} value={String(g)}>{g}</ToggleGroupItem>)}
          </ToggleGroup>
          {cams.length > 1 ? (
            <ToggleGroup spacing={0} size="sm" variant="outline" value={[camSel]} aria-label={t('lab.wall.sensor')}
              onValueChange={(v: unknown[]) => typeof v[0] === 'string' && setCam(v[0])}>
              {cams.map((c) => <ToggleGroupItem key={c} value={c}>{c}</ToggleGroupItem>)}
            </ToggleGroup>
          ) : null}
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[mode]} aria-label={t('lab.wall.mode')}
            onValueChange={(v: unknown[]) => (v[0] === 'rgb' || (v[0] === 'depth' && hasDepth)) && setMode(v[0])}>
            <ToggleGroupItem value="rgb">{t('lab.wall.rgb')}</ToggleGroupItem>
            <ToggleGroupItem value="depth" disabled={!hasDepth}>{t('lab.wall.depth')}</ToggleGroupItem>
          </ToggleGroup>
          {canEval ? (
            <Button size="sm" variant={evalView ? 'secondary' : 'outline'} aria-pressed={evalView} onClick={() => setEvalWanted(!evalView)}>
              <Icon icon="layer.bbox" data-icon="inline-start" />
              {t('lab.wall.evalView')}
            </Button>
          ) : null}
        </div>
      </header>
      <div className="flex min-h-0 flex-1">
      <div ref={box} className="min-h-0 flex-1 overflow-hidden p-4">
        {run.isPending ? null : !uavs.length ? (
          <PanelEmpty title={t('lab.wall.empty')} description={t('lab.wall.emptyHint')} />
        ) : (
          <div className="grid gap-1" style={{ gridTemplateColumns: `repeat(${L.cols}, ${L.tileW}px)` }}>
            {tiles.map((u) => (
              <WallTile key={`${u}/${camSel}/${mode}`} rid={rid} uav={u} cam={camSel} mode={mode} evalView={evalView} w={L.tileW} h={L.tileH}
                selected={sel === u}
                onSelect={() => setSel(u)} onZoom={() => setZoom(zoom ? null : u)} onSpan={onSpan} />
            ))}
          </div>
        )}
      </div>
      {evalView && run.data?.event ? <EventPlan rid={rid} event={run.data.event} fleet={run.data.fleet ?? []} /> : null}
      </div>
      <footer className="flex items-center gap-3 border-t px-4 py-2">
        <Button size="sm" variant="outline" aria-label={ph.playing ? t('lab.wall.pause') : t('lab.wall.play')} onClick={() => (ph.playing ? ph.pause() : ph.play())}>
          <Icon icon={ph.playing ? 'tl.pause' : 'tl.play'} />
        </Button>
        <Button size="sm" variant="ghost" aria-label={t('lab.wall.stepBack')} onClick={() => ph.seek(ph.tS - 0.1)}><Icon icon="tl.stepback" /></Button>
        <Button size="sm" variant="ghost" aria-label={t('lab.wall.stepFwd')} onClick={() => ph.seek(ph.tS + 0.1)}><Icon icon="tl.stepfwd" /></Button>
        <Slider className="flex-1" min={0} max={Math.max(ph.durationS, 0.1)} step={0.1} value={[ph.tS]} aria-label={t('lab.wall.timeline')}
          onValueChange={(v) => ph.seek(Array.isArray(v) ? Number(v[0]) : Number(v))} />
        <span className="font-mono text-hud-sub tabular-nums">{`T+${clock(ph.tS)} / ${clock(ph.durationS)}`}</span>
        {L.pages > 1 ? (
          <span className="inline-flex items-center gap-1">
            <Button size="sm" variant="ghost" disabled={pg === 0} aria-label={t('lab.wall.prev')} onClick={() => setPage(pg - 1)}><Icon icon="chev.left" /></Button>
            <span className="text-hud-sub tabular-nums">{t('lab.wall.page', { p: pg + 1, n: L.pages })}</span>
            <Button size="sm" variant="ghost" disabled={pg >= L.pages - 1} aria-label={t('lab.wall.next')} onClick={() => setPage(pg + 1)}><Icon icon="chev.right" /></Button>
          </span>
        ) : null}
      </footer>
    </section>
  )
}
