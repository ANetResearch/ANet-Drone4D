// /lab/replay，双运行同步回放（D3-AC-19"两个 Run 同步对比时时间轴一致"、M27-AC-009"同一 t_sim_ns 下观测帧时间戳差 ≤ 1 个帧周期"；
// D3-AC-56"样例包在内部工作区可浏览"；AWR-14 §21.7、§21.13）。内部 D3 构建，最低角色研究者（帧接口 BN-49、BN-51）。
// - 运行 A 与 B 各一栏，每栏为该运行各机同一成像传感器的私有视角（存档帧）；传感器只列两侧共有的；选中某机时两栏同机高亮；
// - 统一时间轴：lab 播放头（stores/lab/playhead.ts）是唯一时钟，两栏所取帧都由同一时刻 u 按 sync.ts 算出；对齐方式缺省为同一
//   仿真时刻（`sim`），可切换为按事件起点对齐（`event`，两侧都有事件偏移时可用）；时间轴跨两侧全部通道的首帧至末帧；
// - 同步读数：当前时刻各机两侧帧时间戳之差的最大值与帧周期（超出时为表格外唯一的强调色）；全程检查在重叠区间内逐帧时刻求最大偏差；
// - 样例包：两侧未选齐时在页内列出，选齐后经"样例包"侧栏打开；样例行可设为运行 A 或 B；
// - URL：`a`、`b`、`cam`、`align`、`rate` 驱动界面（replaceState），`t` 以 1 Hz 静默写回（§21.2.2 规则 3）。
import { useQueries, useQuery, type UseQueryResult } from '@tanstack/react-query'
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate, updateSearch, type RouteProps } from '@/app/router/router'
import { sanitizeText } from '@/lib/sanitize'
import { benchGet, frameIndex, ROLE_RANK, type BenchRun, type BenchRunChannel, type BenchRunEvaluation, type FrameIndexItem } from '@/net/bench'
import { usePlayhead } from '@/stores/lab/playhead'
import { PanelEmpty } from '@/ui/brand'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Slider } from '@/ui/components/ui/slider'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { fmtNum } from '@/ui/lab/board'
import type { RunsPage } from '@/ui/lab/runs/runs'
import { useBenchSession } from '@/ui/lab/useBenchSession'
import { layoutWall } from '@/ui/lab/wall/layout'
import { useDocTitle } from '@/ui/lab/worlds/LabHeader'
import { useWindowSize } from '@/ui/layout/layoutState'
import { useElementWidth } from '@/ui/layout/useElementWidth'
import { ReplayTile } from './ReplayTile'
import { parseSamplePacks, type SamplePack } from './samplePack'
import { SamplePackPanel } from './SamplePackPanel'
import {
  ALIGN_MODES, alignShifts, assignRun, clockText, commonCams, msText, parseReplaySearch, RATES, replayChannels, signedS, syncAt, sweepSync, tracksOf,
  unifiedSpan, type AlignMode, type RunSide, type SideTracks, type SyncState,
} from './sync'

/** 运行选择列表（BN-42，一页 500 条，只列有帧数据的运行） */
export const REPLAY_RUNS_QUERY = '?limit=500'
export const replayRunsKey = ['lab', 'runs', REPLAY_RUNS_QUERY] as const
export const samplesKey = ['lab', 'samples'] as const
type ReplayRun = BenchRun & { policy?: string | null; evaluation?: BenchRunEvaluation | null }
export const replayRunKey = (rid: string) => ['lab', 'run', rid] as const
export const frameIndexKey = (rid: string, uav: string, cam: string) => ['lab', 'fidx', rid, uav, cam] as const

/** 帧索引查询的合并结果（模块级函数，TanStack Query 在各查询结果不变时复用上一次的返回值） */
function combineIndex(rs: readonly UseQueryResult<{ periodNs: number; items: FrameIndexItem[] }>[]) {
  return { data: rs.map((r) => r.data), pending: rs.some((r) => r.isPending) }
}

/** 双栏布局的最小容器宽度（px）；更窄时两栏上下排列 */
const SIDE_BY_SIDE_MIN_W = 1100
/** URL 中播放头的写回周期（ms，§21.2.2 规则 3） */
const URL_WRITE_MS = 1000

function SyncReadout({ sync }: { sync: SyncState }) {
  const t = useT()
  if (sync.maxAbsDeltaNs === null) return <span className="text-hud-sub text-muted-foreground" data-sync-ok="">{t('lab.replay.sync.none')}</span>
  return (
    <span className={`inline-flex items-center gap-1 font-mono text-hud-sub tabular-nums ${sync.ok ? '' : 'text-brand-text'}`}
      data-sync-ok={String(sync.ok)} data-sync-delta-ns={sync.maxAbsDeltaNs}>
      <Icon icon={sync.ok ? 'check' : 'alert.warning'} />
      {t(sync.ok ? 'lab.replay.sync.ok' : 'lab.replay.sync.over', { d: msText(sync.maxAbsDeltaNs), p: msText(sync.periodNs), n: sync.compared })}
    </span>
  )
}

function RunPicker({ side, value, options, onPick }: { side: RunSide; value: string | null; options: readonly { id: string; label: string }[]; onPick: (rid: string | null) => void }) {
  const t = useT()
  const opts = value && !options.some((o) => o.id === value) ? [{ id: value, label: value }, ...options] : options
  return (
    <label className="flex flex-col gap-1 text-hud-sub">
      <span className="text-muted-foreground">{t(`lab.replay.pick.${side}`)}</span>
      <NativeSelect size="sm" value={value ?? ''} data-replay-pick={side} className="min-w-72" onChange={(e) => onPick(e.target.value || null)}>
        <NativeSelectOption value="">{t('lab.replay.pick.none')}</NativeSelectOption>
        {opts.map((o) => <NativeSelectOption key={o.id} value={o.id}>{o.label}</NativeSelectOption>)}
      </NativeSelect>
    </label>
  )
}

interface PaneProps {
  side: RunSide
  rid: string | null
  run: ReplayRun | undefined
  failed: boolean
  cam: string | null
  side_: SideTracks
  sync: SyncState
  pending: boolean
  w: number
  h: number
  sel: string | null
  onSelect: (uav: string) => void
}

function RunPane({ side, rid, run, failed, cam, side_, sync, pending, w, h, sel, onSelect }: PaneProps) {
  const t = useT()
  const L = layoutWall(side_.tracks.length, w, h)
  const ec = run?.evaluation?.components.ec
  return (
    <div data-replay-pane={side} data-run={rid ?? ''} className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-hud-sub">
        <Badge variant="outline" className="font-mono">{side.toUpperCase()}</Badge>
        <span className="font-mono font-bold">{rid ? sanitizeText(rid, 64) : '—'}</span>
        {run ? <span className="font-mono text-muted-foreground">{sanitizeText(run.event_id, 64)}</span> : null}
        {run?.policy ? <span className="font-mono text-muted-foreground">{sanitizeText(run.policy, 48)}</span> : null}
        {ec !== undefined && ec !== null ? <span className="font-mono">{t('lab.replay.paneEc', { v: fmtNum(ec) })}</span> : null}
        {side_.shiftNs ? <span className="font-mono text-muted-foreground" data-replay-shift={side_.shiftNs}>{t('lab.replay.shift', { s: signedS(side_.shiftNs) })}</span> : null}
        {rid ? (
          <Button size="xs" variant="ghost" className="ml-auto" onClick={() => navigate(`/lab/runs/${encodeURIComponent(rid)}`)}>
            <Icon icon="sensor.camera" data-icon="inline-start" />
            {t('lab.replay.openWall')}
          </Button>
        ) : null}
      </div>
      {!rid ? (
        <PanelEmpty title={t(`lab.replay.pickHint.${side}`)} />
      ) : failed ? (
        <PanelEmpty title={t('lab.replay.runUnavailable', { id: rid })} description={t('lab.unreachableHint')} />
      ) : pending ? null : !cam || !side_.tracks.length ? (
        <PanelEmpty title={t('lab.replay.noCam')} description={t('lab.replay.noCamHint')} />
      ) : (
        <div className="grid gap-1" style={{ gridTemplateColumns: `repeat(${L.cols}, ${L.tileW}px)` }}>
          {side_.tracks.slice(0, L.cap).map((tr) => {
            const pair = sync.pairs.find((p) => p.uav === tr.uav)
            const pick = pair?.[side]
            return pick ? (
              <ReplayTile key={`${rid}/${tr.uav}/${cam}`} side={side} rid={rid} cam={cam} track={tr} pick={pick} w={L.tileW} h={L.tileH}
                selected={sel === tr.uav} onSelect={() => onSelect(tr.uav)} />
            ) : null
          })}
        </div>
      )}
    </div>
  )
}

/**
 * 两侧帧索引与统一时间轴：通道（replayChannels）→ 帧索引查询（BN-49，与观测墙格同一查询键）→ 轨道 → 区间与全程同步检查。
 * 只依赖原始值与查询结果引用，帧索引不变时轨道、区间与全程检查不重算。
 */
function useReplayTracks(ridA: string | null, framesA: readonly BenchRunChannel[] | undefined, ridB: string | null,
  framesB: readonly BenchRunChannel[] | undefined, cam: string | null, align: { a: number; b: number }) {
  const chans = React.useMemo(() => replayChannels({ rid: ridA, frames: framesA }, { rid: ridB, frames: framesB }, cam), [ridA, ridB, framesA, framesB, cam])
  const idx = useQueries({
    queries: chans.map((c) => ({
      queryKey: frameIndexKey(c.rid, c.uav, cam ?? ''), queryFn: ({ signal }: { signal: AbortSignal }) => frameIndex(c.rid, c.uav, cam ?? '', signal), staleTime: Infinity,
    })),
    combine: combineIndex,
  })
  const shiftA = align.a
  const shiftB = align.b
  const idxData = idx.data
  const sides = React.useMemo(() => {
    const tr = tracksOf(chans, idxData)
    const sa: SideTracks = { shiftNs: shiftA, tracks: tr.a }
    const sb: SideTracks = { shiftNs: shiftB, tracks: tr.b }
    return { sideA: sa, sideB: sb, span: unifiedSpan([sa, sb]), sweep: sweepSync(sa, sb) }
  }, [chans, idxData, shiftA, shiftB])
  return { idxPending: idx.pending, ...sides }
}

export function ReplayPage({ search }: RouteProps) {
  const t = useT()
  const session = useBenchSession()
  const q = parseReplaySearch(search)
  useDocTitle(t('lab.replay.docTitle'))
  const canRead = session !== null && ROLE_RANK[session.role] >= ROLE_RANK.researcher
  const runs = useQuery({ queryKey: replayRunsKey, queryFn: () => benchGet<RunsPage>(`/runs${REPLAY_RUNS_QUERY}`), enabled: canRead, staleTime: 30_000 })
  const packs = useQuery({
    queryKey: samplesKey, queryFn: async (): Promise<SamplePack[]> => parseSamplePacks(await benchGet<unknown>('/samples')),
    enabled: canRead, retry: false, staleTime: 60_000,
  })
  const runQ = (rid: string | null) => ({
    queryKey: replayRunKey(rid ?? ''), queryFn: () => benchGet<ReplayRun>(`/runs/${encodeURIComponent(rid ?? '')}`), enabled: canRead && rid !== null,
  })
  const runA = useQuery(runQ(q.a))
  const runB = useQuery(runQ(q.b))
  const cams = commonCams(q.a ? (runA.data?.frames ?? []) : undefined, q.b ? (runB.data?.frames ?? []) : undefined)
  const cam = q.cam && cams.includes(q.cam) ? q.cam : (cams[0] ?? null)
  const align = alignShifts(q.align, runA.data?.event?.offset_s, runB.data?.event?.offset_s)
  const { idxPending, sideA, sideB, span, sweep } = useReplayTracks(q.a, runA.data?.frames, q.b, runB.data?.frames, cam, align)

  const ph = usePlayhead()
  React.useEffect(() => {
    usePlayhead.setState({ tS: q.t ?? 0, durationS: q.t ?? 0, playing: false, rate: q.rate })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // 时间轴跨两侧全部通道；空闲的播放头早于首帧时移到首帧
  React.useEffect(() => {
    if (!span) return
    const st = usePlayhead.getState()
    st.setDuration(span.endS)
    if (!st.playing && st.tS < span.startS) st.seek(span.startS)
  }, [span])
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
  React.useEffect(() => {
    const h = setInterval(() => {
      const v = usePlayhead.getState().tS.toFixed(2)
      if (new URLSearchParams(location.search).get('t') !== v) updateSearch({ t: v }, { silent: true })
    }, URL_WRITE_MS)
    return () => clearInterval(h)
  }, [])

  const sync = syncAt(sideA, sideB, Math.round(ph.tS * 1e9))
  const [sel, setSel] = React.useState<string | null>(null)
  const [packOpen, setPackOpen] = React.useState(false)
  const [box, bw] = useElementWidth<HTMLDivElement>(1280)
  const { h: wh } = useWindowSize()
  const both = q.a !== null && q.b !== null
  const sideBySide = bw >= SIDE_BY_SIDE_MIN_W
  const paneW = sideBySide ? Math.floor((bw - 16) / 2) : bw
  const paneH = Math.max(200, sideBySide ? wh - 300 : (wh - 340) / 2)
  const assign = (side: RunSide, rid: string | null) => {
    updateSearch(assignRun(q, side, rid))
    setPackOpen(false)
  }
  const options = (runs.data?.items ?? []).filter((r) => r.has_frames).map((r) => ({
    id: r.run_id, label: [r.run_id, r.event_id, r.policy ?? r.kind].filter(Boolean).join(' · '),
  }))
  const packsUnavailable = packs.isError
  const eventsDiffer = runA.data && runB.data && runA.data.event_id !== runB.data.event_id
  const canEventAlign = typeof runA.data?.event?.offset_s === 'number' && typeof runB.data?.event?.offset_s === 'number'

  if (session !== null && !canRead) {
    return (
      <section data-view="lab-replay" className="app-layer-overlay-page bg-background p-4">
        <PanelEmpty title={t('lab.replay.roleDenied')} description={t('lab.replay.roleDeniedHint')}
          action={<Button size="sm" variant="outline" onClick={() => navigate('/lab/board')}>{t('lab.replay.toBoard')}</Button>} />
      </section>
    )
  }
  const pack = (
    <SamplePackPanel packs={packs.data} pending={canRead && packs.isPending} unavailable={packsUnavailable} a={q.a} b={q.b} onAssign={assign} />
  )
  return (
    <section data-view="lab-replay" data-figure="lab-replay" className="app-layer-overlay-page flex flex-col bg-background">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <Button size="sm" variant="ghost" onClick={() => navigate('/lab/runs')}>
          <Icon icon="chev.left" data-icon="inline-start" />
          {t('lab.replay.back')}
        </Button>
        <Icon icon="tl.replay" />
        <h1 className="text-ed-title font-bold">{t('lab.replay.title')}</h1>
        <span className="text-hud-sub text-muted-foreground">{session ? t('lab.session', { name: session.principal, role: t(`lab.role.${session.role}`) }) : ''}</span>
        {both ? (
          <Button size="sm" variant="outline" className="ml-auto" onClick={() => setPackOpen(true)} data-replay-pack-open="">
            <Icon icon="data.package" data-icon="inline-start" />
            {t('lab.replay.pack.title')}
          </Button>
        ) : null}
      </header>
      <div className="flex flex-wrap items-end gap-3 border-b px-4 py-2" data-figure="lab-replay-controls">
        <RunPicker side="a" value={q.a} options={options} onPick={(r) => assign('a', r)} />
        <Button size="icon-sm" variant="ghost" aria-label={t('lab.replay.swap')} disabled={!q.a && !q.b} onClick={() => updateSearch({ a: q.b, b: q.a })} data-replay-swap="">
          <Icon icon="chev.updown" className="rotate-90" />
        </Button>
        <RunPicker side="b" value={q.b} options={options} onPick={(r) => assign('b', r)} />
        {cams.length > 1 ? (
          <label className="flex flex-col gap-1 text-hud-sub">
            <span className="text-muted-foreground">{t('lab.replay.sensor')}</span>
            <ToggleGroup spacing={0} size="sm" variant="outline" value={[cam ?? '']} aria-label={t('lab.replay.sensor')}
              onValueChange={(v: unknown[]) => typeof v[0] === 'string' && updateSearch({ cam: v[0] })}>
              {cams.map((c) => <ToggleGroupItem key={c} value={c}>{c}</ToggleGroupItem>)}
            </ToggleGroup>
          </label>
        ) : null}
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.replay.align')}</span>
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[align.mode]} aria-label={t('lab.replay.align')} data-replay-align={align.mode}
            onValueChange={(v: unknown[]) => (ALIGN_MODES as readonly unknown[]).includes(v[0]) && updateSearch({ align: v[0] === 'sim' ? null : (v[0] as AlignMode) })}>
            <ToggleGroupItem value="sim">{t('lab.replay.alignSim')}</ToggleGroupItem>
            <ToggleGroupItem value="event" disabled={!canEventAlign}>{t('lab.replay.alignEvent')}</ToggleGroupItem>
          </ToggleGroup>
        </label>
        <div className="ml-auto flex flex-col items-end gap-0.5 text-hud-sub" data-replay-sweep={String(sweep.ok)} data-sweep-max-ns={sweep.maxAbsDeltaNs ?? ''}>
          {sweep.ok === null ? null : (
            <span className={`inline-flex items-center gap-1 font-mono tabular-nums ${sweep.ok ? 'text-muted-foreground' : ''}`}>
              <Icon icon={sweep.ok ? 'check' : 'alert.warning'} />
              {t(sweep.ok ? 'lab.replay.sweep.ok' : 'lab.replay.sweep.over', {
                n: sweep.points, d: msText(sweep.maxAbsDeltaNs), p: msText(Math.max(...sweep.pairs.map((x) => x.periodNs))),
              })}
            </span>
          )}
          {eventsDiffer ? <span className="text-muted-foreground" data-replay-note="events">{t('lab.replay.eventsDiffer')}</span> : null}
          {q.align === 'event' && align.mode === 'sim' && both && runA.data && runB.data ? (
            <span className="text-muted-foreground" data-replay-note="align">{t('lab.replay.alignFallback')}</span>
          ) : null}
        </div>
      </div>
      <div ref={box} className="min-h-0 flex-1 overflow-auto p-4">
        <div className={sideBySide ? 'grid grid-cols-2 gap-4' : 'flex flex-col gap-4'}>
          <RunPane side="a" rid={q.a} run={runA.data} failed={runA.isError} cam={cam} side_={sideA} sync={sync} pending={runA.isPending || idxPending}
            w={paneW} h={paneH} sel={sel} onSelect={setSel} />
          <RunPane side="b" rid={q.b} run={runB.data} failed={runB.isError} cam={cam} side_={sideB} sync={sync} pending={runB.isPending || idxPending}
            w={paneW} h={paneH} sel={sel} onSelect={setSel} />
        </div>
        {!both ? <div className="mt-6">{pack}</div> : null}
      </div>
      <footer className="flex flex-wrap items-center gap-3 border-t px-4 py-2" data-figure="lab-replay-timeline">
        <Button size="sm" variant="outline" disabled={!span} aria-label={ph.playing ? t('lab.replay.pause') : t('lab.replay.play')} onClick={() => (ph.playing ? ph.pause() : ph.play())}>
          <Icon icon={ph.playing ? 'tl.pause' : 'tl.play'} />
        </Button>
        <Button size="sm" variant="ghost" disabled={!span} aria-label={t('lab.replay.stepBack')} onClick={() => ph.seek(ph.tS - 0.1)}><Icon icon="tl.stepback" /></Button>
        <Button size="sm" variant="ghost" disabled={!span} aria-label={t('lab.replay.stepFwd')} onClick={() => ph.seek(ph.tS + 0.1)}><Icon icon="tl.stepfwd" /></Button>
        <NativeSelect size="sm" value={String(ph.rate)} aria-label={t('lab.replay.rate')} data-replay-rate=""
          onChange={(e) => {
            const r = Number(e.target.value)
            ph.setRate(r)
            updateSearch({ rate: r === 1 ? null : String(r) }, { silent: true })
          }}>
          {RATES.map((r) => <NativeSelectOption key={r} value={String(r)}>{`×${r}`}</NativeSelectOption>)}
        </NativeSelect>
        <Slider className="min-w-48 flex-1" min={0} max={Math.max(ph.durationS, 0.1)} step={0.05} value={[ph.tS]} aria-label={t('lab.replay.timeline')}
          disabled={!span} onValueChange={(v) => ph.seek(Array.isArray(v) ? Number(v[0]) : Number(v))} />
        <span className="font-mono text-hud-sub tabular-nums" data-replay-clock={ph.tS.toFixed(2)}>{`T+${clockText(ph.tS)} / ${clockText(ph.durationS)}`}</span>
        <SyncReadout sync={sync} />
      </footer>
      <Sheet open={packOpen && both} onOpenChange={(o) => setPackOpen(o)}>
        <SheetContent side="right" className="w-240 max-w-full overflow-auto sm:max-w-240" data-replay-pack-sheet="">
          <SheetHeader className="pb-0">
            <SheetTitle>{t('lab.replay.pack.title')}</SheetTitle>
            <SheetDescription>{t('lab.replay.pack.description')}</SheetDescription>
          </SheetHeader>
          <div className="p-4">{pack}</div>
        </SheetContent>
      </Sheet>
    </section>
  )
}

