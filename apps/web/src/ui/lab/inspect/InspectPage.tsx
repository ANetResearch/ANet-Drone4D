// /lab/runs/:rid/inspect，运行回放 · 数据检视（AWR-14 §21.9；AWR-05 §6.1.6；D3-AC-25）。内部工作区，最低角色研究者；评测 catalog
// 需评测员及以上（角色不足时回到分发 catalog 并提示 115）。布局：Resizable 左右分栏（缺省 45% / 55%，最小 360 px）——左侧场景总览，
// 右侧 Rerun Web Viewer（按需加载）；底部为 lab 时间线（播放、步进、拖动），Viewer 播放时显示"由数据检视驱动"。
// 数据：BN-57 viewer_url（设置路径限定的 d3_rr Cookie，URL 不含令牌）→ Viewer 经 rerun-proxy 按需取 Chunk；运行清单（机队航迹）与
// 评测员的事件磁带（真值演员）取自 bench-api。查询参数 `t`（打开后 seek，播放头 1 Hz 写回）、`slice`（`<from>,<to>` 仿真秒）、
// `catalog ∈ {dist, eval}`。
import { useQuery } from '@tanstack/react-query'
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate, updateSearch, type RouteProps } from '@/app/router/router'
import { TapeReader } from '@/engine'
import { benchBytes, benchGet, ROLE_RANK, type BenchRun } from '@/net/bench'
import { usePlayhead } from '@/stores/lab/playhead'
import { PanelEmpty } from '@/ui/brand'
import { Button } from '@/ui/components/ui/button'
import { Kbd } from '@/ui/components/ui/kbd'
import { Popover, PopoverContent, PopoverDescription, PopoverHeader, PopoverTitle, PopoverTrigger } from '@/ui/components/ui/popover'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/ui/components/ui/resizable'
import { Slider } from '@/ui/components/ui/slider'
import { toast } from '@/ui/components/ui/toast'
import { Icon } from '@/ui/icons/Icon'
import { useBenchSession } from '@/ui/lab/useBenchSession'
import { inspectSupport, nativeCommand, parseInspectQuery, viewerUrlPath, type CatalogKind, type CatalogSource, type ViewerLink } from './inspect'
import type { InspectController } from './inspectController'
import { InspectPanel, useInspectSnapshot } from './InspectPanel'
import { labPlayheadPort } from './playheadPort'
import type { LabSelection } from './rerunSync'
import { ScenePane, type SceneTape } from './ScenePane'
import { RERUN_VIEWER_VERSION } from './viewerLoader'

const clock = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${(s % 60).toFixed(1).padStart(4, '0')}`

export function InspectPage({ params, search }: RouteProps) {
  const t = useT()
  const rid = params.rid!
  const session = useBenchSession()
  const q0 = React.useMemo(() => parseInspectQuery(search), []) // eslint-disable-line react-hooks/exhaustive-deps
  const canEval = session !== null && ROLE_RANK[session.role] >= ROLE_RANK.evaluator
  const [want, setWant] = React.useState<CatalogKind>(q0.catalog)
  const catalog: CatalogKind = want === 'eval' && canEval ? 'eval' : 'dist'
  const denied = want === 'eval' && session !== null && !canEval
  React.useEffect(() => {
    // catalog=eval 而角色不足：回到分发 catalog 并提示一次（AWR-14 §21.2.2 规则 2 的同一做法）
    if (denied) toast.add({ type: 'info', title: t('lab.inspect.evalDenied') })
  }, [denied, t])
  const run = useQuery({ queryKey: ['lab', 'run', rid], queryFn: () => benchGet<BenchRun>(`/runs/${encodeURIComponent(rid)}`), enabled: session !== null })
  const tape = useQuery({
    queryKey: ['lab', 'tape', rid],
    queryFn: async ({ signal }): Promise<SceneTape> => {
      const b = await benchBytes(`/runs/${encodeURIComponent(rid)}/truth/event/tape`, signal)
      const off = Number(b.headers.get('X-Event-Offset-Ns') ?? Math.round((run.data?.event?.offset_s ?? 0) * 1e9))
      return { r: new TapeReader(b.buf), offNs: off }
    },
    enabled: catalog === 'eval' && !!run.data?.event,
    staleTime: Infinity,
    retry: false,
  })
  const support = React.useMemo(() => inspectSupport(search), []) // eslint-disable-line react-hooks/exhaustive-deps
  const source = React.useMemo<CatalogSource>(() => ({
    kind: 'catalog', catalog,
    fetchLink: (signal) => benchGet<ViewerLink>(viewerUrlPath(rid, { slice: q0.slice, catalog }), signal),
  }), [rid, catalog, q0.slice])

  const ph = usePlayhead()
  const [ctl, setCtl] = React.useState<InspectController | null>(null)
  const [sel, setSel] = React.useState<LabSelection | null>(null)

  React.useEffect(() => {
    document.title = t('lab.inspect.docTitle', { rid })
    usePlayhead.setState({ tS: q0.t ?? 0, playing: false, durationS: q0.t ?? 0 })
  }, [rid]) // eslint-disable-line react-hooks/exhaustive-deps
  React.useEffect(() => {
    // 本地时钟（公开构建与内部回放在 P0b 都以本地时钟驱动，AWR-14 §21.13 static 来源）；Viewer 播放时由同步器暂停
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
    // 播放头写回 URL `t`（replaceState，1 Hz，§21.2.2 规则 3）
    const h = setInterval(() => updateSearch({ t: usePlayhead.getState().tS.toFixed(2) }, { silent: true }), 1000)
    return () => clearInterval(h)
  }, [])

  const onRange = React.useCallback((a: number, b: number) => {
    const st = usePlayhead.getState()
    if (b > st.durationS) st.setDuration(b)
    if (q0.t === null && !st.playing && st.tS < a) st.seek(a)
  }, [q0.t])
  const pickScene = (s: LabSelection) => {
    setSel(s)
    ctl?.selectInViewer(s, catalog === 'eval')
  }
  const switchCatalog = (c: CatalogKind) => {
    setWant(c)
    updateSearch({ catalog: c === 'dist' ? null : c }, { silent: true })
  }
  const cmd = nativeCommand(rid, ph.tS, catalog)
  const native = <NativeViewer cmd={cmd} t={t} />

  if (run.isError) {
    return (
      <section className="app-layer-overlay-page bg-background p-4">
        <PanelEmpty title={t('lab.inspect.unavailable')} description={t('lab.unreachableHint')}
          action={<Button size="sm" variant="outline" onClick={() => navigate(`/lab/runs/${encodeURIComponent(rid)}`)}>{t('lab.inspect.back')}</Button>} />
      </section>
    )
  }
  return (
    <section data-view="lab-inspect" data-inspect-catalog={catalog} className="app-layer-overlay-page flex flex-col bg-background">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <Button size="sm" variant="ghost" onClick={() => navigate(`/lab/runs/${encodeURIComponent(rid)}`)}>
          <Icon icon="chev.left" data-icon="inline-start" />
          {t('lab.inspect.back')}
        </Button>
        <h1 className="text-ed-title font-bold">{t('lab.inspect.title')}</h1>
        <span className="font-mono text-hud-sub">{rid}</span>
        <span className="text-hud-sub text-muted-foreground">{t('lab.inspect.catalogLabel', { name: t(`lab.inspect.catalog.${catalog}`) })}</span>
        {catalog === 'eval' ? (
          <span data-eval-tag="" className="rounded-sm border border-foreground px-1.5 text-hud-sub font-bold">{t('lab.inspect.evalTag')}</span>
        ) : null}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {canEval ? (
            <Button size="sm" variant="outline" onClick={() => switchCatalog(catalog === 'eval' ? 'dist' : 'eval')} data-inspect-catalog-switch="">
              <Icon icon="layer.bbox" data-icon="inline-start" />
              {t(catalog === 'eval' ? 'lab.inspect.openDist' : 'lab.inspect.openEval')}
            </Button>
          ) : null}
          {native}
        </div>
      </header>
      <ResizablePanelGroup orientation="horizontal" className="min-h-0 flex-1">
        <ResizablePanel defaultSize="45%" minSize="360px">
          <ScenePane fleet={run.data?.fleet ?? []} tape={catalog === 'eval' ? (tape.data ?? null) : null} tS={ph.tS} durS={ph.durationS}
            selection={sel} onSelect={pickScene} t={t} />
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel defaultSize="55%" minSize="360px">
          <div className="flex size-full flex-col">
            <InspectPanel source={source} playhead={labPlayheadPort} support={support} t={t} fallback={native}
              onSelect={setSel} onRange={onRange} onController={setCtl} />
            <p className="border-t px-3 py-1.5 text-hud-sub text-muted-foreground">
              {t(catalog === 'eval' ? 'lab.inspect.note.eval' : 'lab.inspect.note.dist', { version: RERUN_VIEWER_VERSION })}
            </p>
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
      <InspectTimeline ctl={ctl} t={t} />
    </section>
  )
}

/** lab 时间线（播放、步进、拖动）；拖动的开始与结束转给同步器（拖动中推送 ≤ 10 Hz，结束时补推精确时刻） */
function InspectTimeline({ ctl, t }: { ctl: InspectController | null; t: (k: string, p?: Record<string, string | number>) => string }) {
  const ph = usePlayhead()
  const scrub = React.useRef(false)
  return (
    <footer className="flex items-center gap-3 border-t px-4 py-2" data-inspect-timeline="">
      <Button size="sm" variant="outline" aria-label={ph.playing ? t('lab.wall.pause') : t('lab.wall.play')} onClick={() => (ph.playing ? ph.pause() : ph.play())}>
        <Icon icon={ph.playing ? 'tl.pause' : 'tl.play'} />
      </Button>
      <Button size="sm" variant="ghost" aria-label={t('lab.wall.stepBack')} onClick={() => ph.seek(ph.tS - 0.1)}><Icon icon="tl.stepback" /></Button>
      <Button size="sm" variant="ghost" aria-label={t('lab.wall.stepFwd')} onClick={() => ph.seek(ph.tS + 0.1)}><Icon icon="tl.stepfwd" /></Button>
      <Slider className="flex-1" min={0} max={Math.max(ph.durationS, 0.1)} step={0.1} value={[ph.tS]} aria-label={t('lab.wall.timeline')}
        onValueChange={(v) => {
          if (!scrub.current) {
            scrub.current = true
            ctl?.setScrubbing(true)
          }
          ph.seek(Array.isArray(v) ? Number(v[0]) : Number(v))
        }}
        onValueCommitted={() => {
          scrub.current = false
          ctl?.setScrubbing(false)
        }} />
      <span className="font-mono text-hud-sub tabular-nums">{`T+${clock(ph.tS)} / ${clock(ph.durationS)}`}</span>
      {ctl ? <DrivenBy ctl={ctl} t={t} /> : null}
    </footer>
  )
}

function DrivenBy({ ctl, t }: { ctl: InspectController; t: (k: string) => string }) {
  const snap = useInspectSnapshot(ctl)
  return snap.source === 'rerun' ? (
    <span data-inspect-driven="" className="rounded-sm border border-foreground px-1.5 text-hud-sub">{t('lab.inspect.drivenBy')}</span>
  ) : null
}

/** "在原生 Viewer 中打开"：显示命令（本机代理注入鉴权，17 §18.10.3），可复制 */
function NativeViewer({ cmd, t }: { cmd: string; t: (k: string) => string }) {
  return (
    <Popover>
      <PopoverTrigger render={<Button size="sm" variant="outline" data-inspect-native="" />}>
        <Icon icon="data.export" data-icon="inline-start" />
        {t('lab.inspect.native')}
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[30rem]">
        <PopoverHeader>
          <PopoverTitle>{t('lab.inspect.native')}</PopoverTitle>
          <PopoverDescription>{t('lab.inspect.nativeHint')}</PopoverDescription>
        </PopoverHeader>
        <Kbd className="pointer-events-auto h-auto w-full justify-start px-2 py-1 font-mono text-hud-sub break-all select-all" data-inspect-native-cmd="">{cmd}</Kbd>
        <Button size="sm" variant="outline" onClick={() => {
          void navigator.clipboard?.writeText(cmd).then(() => toast.add({ type: 'success', title: t('lab.inspect.copied') }))
        }}>{t('lab.inspect.copy')}</Button>
      </PopoverContent>
    </Popover>
  )
}
