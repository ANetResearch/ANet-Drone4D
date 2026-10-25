// 公开站样例运行页 `/lab/samples/:sid`（AWR-14 §21.29.3、§21.14；AWR-05 §7.9；D3-AC-21、D3-AC-25 公开样例页条款；UX-FR-221、
// UX-AC-113）。Owner: M15.
// 布局（≥ lg 两栏，以下单列，§21.14.3）：
//   顶部   返回样例目录、标题、"样例运行，不构成排名"、样例说明（事件、世界、策略、机队、观测档）
//   左栏   事件回放（前端磁带解码，只显示脚本演员；机队取 tracks_obs.bin）
//   右栏   多机私有视角（RGB 与热成像伪彩视频，按播放头同步）；"样例数据检视"切换为 Rerun Web Viewer 面板（Tier B/A 读取 public 档
//          切片与 basemap.rrd，不发起 catalog 请求；Tier S、手机与平板显示切片下载，§21.29.3 规则 2）
//   底部   lab 时间线（播放、步进、拖动、速率；缓冲中提示）；样例数据下载表（视频、切片、底图；> 64 MB 的经分发渠道）
// 规则：
// 1. 只发起 /lab/ 与 /lab-static/ 的静态 GET（磁带与轨迹经 labGet；视频与切片为同源静态地址；Viewer 的 Wasm 在 /lab/assets/）；
//    页面挂载期间的请求守卫（staticGuard.ts）拒绝其余请求，包括 Viewer 内置的使用统计上报；不建立 WS，不显示得分、分量与任何
//    跨策略比较（metrics 路径不读取）；
// 2. lab 播放头（stores/lab/playhead）是唯一时间来源：本页每个动画帧推进播放头并驱动全部视频；数据检视面板经 labPlayheadPort 与
//    播放头双向同步（§21.13）；
// 3. 查询参数 `t`（仿真秒）为打开时的播放头时刻。
import * as React from 'react'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Slider } from '@/ui/components/ui/slider'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { usePlayhead } from '@/stores/lab/playhead'
import { decideSupport, probeDevice, SOFTWARE_RENDERER_RE, type DeviceProbe, type StaticSource, type Support } from '@/ui/lab/inspect/inspect'
import type { InspectController } from '@/ui/lab/inspect/inspectController'
import { InspectPanel } from '@/ui/lab/inspect/InspectPanel'
import { labPlayheadPort } from '@/ui/lab/inspect/playheadPort'
import { RERUN_VIEWER_VERSION } from '@/ui/lab/inspect/viewerLoader'
import { fmtMb, LabEmpty, tableMaxHeight } from '../pages'
import { LAB_PREFIX, onLabLinkClick, type LabRouteProps } from '../router'
import { artifactSrc, findSample, isHosted, type LabStaticIndex, type LabStaticRun } from '../staticIndex'
import { PrivateViews } from './PrivateViews'
import { SAMPLE_EVENT_OFFSET_S, sampleDurationS, useSampleData } from './sampleData'
import { ScenePlan, type ScenePlanProps } from './ScenePlan'
import { blockedRequests, installStaticGuard } from './staticGuard'
import { useInspectT, useSampleT, type SampleT } from './text'
import { deviceClass, maxConcurrentVideos, VideoSyncGroup, type DeviceClass } from './videoSync'

/** 时间线速率（AWR-14 §21.13 LabPlayhead.rate 的子集） */
export const SAMPLE_RATES = [0.5, 1, 2, 4] as const
/** 同步度量写入页面数据属性的周期（ms） */
const STATS_MS = 250

/** 时刻读数 `MM:SS.s` */
export const clockText = (s: number): string => {
  const ds = Math.round(Math.max(0, s) * 10)
  return `${String(Math.floor(ds / 600)).padStart(2, '0')}:${((ds % 600) / 10).toFixed(1).padStart(4, '0')}`
}

/** 查询参数 `t`（仿真秒，≥ 0）；无效为 null */
export function parseSampleT(search: string): number | null {
  const v = new URLSearchParams(search).get('t')
  if (v === null || v.trim() === '') return null
  const n = Number(v)
  return Number.isFinite(n) && n >= 0 ? n : null
}

/** 数据检视的静态来源：切片与底图（分发渠道副本优先，其次本站托管的文件，ADR-221；recording_id = run_id，AWR-17 §18.8.5 v1.6 注） */
export function inspectSource(index: LabStaticIndex, run: LabStaticRun): StaticSource | null {
  const slices = run.slices.flatMap((s) => {
    const url = artifactSrc(index, s)
    return url ? [{ url, bytes: s.bytes }] : []
  })
  if (!slices.length) return null
  const bm = run.basemap ? artifactSrc(index, { path: run.basemap, bytes: run.basemapBytes, external: run.basemapExternal }) : null
  return { kind: 'static', recordingId: run.runId, slices, basemap: bm ? { url: bm, bytes: run.basemapBytes } : null, wasmUrl: index.viewerWasm?.url ?? null }
}

interface Viewport { cls: DeviceClass; soft: boolean; probe: DeviceProbe }

function useViewport(): Viewport {
  const probe = React.useMemo(() => probeDevice(), [])
  const read = (): string => `${typeof innerWidth === 'number' ? innerWidth : 1280}:${typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches ? 1 : 0}`
  const key = React.useSyncExternalStore((cb) => {
    globalThis.addEventListener?.('resize', cb)
    return () => globalThis.removeEventListener?.('resize', cb)
  }, read, () => '1280:0')
  const [w, c] = key.split(':')
  return { cls: deviceClass(Number(w), c === '1'), soft: SOFTWARE_RENDERER_RE.test(probe.renderer) || (!probe.webgl2 && !probe.webgpu), probe }
}

export function SampleRunPage({ index, params }: LabRouteProps) {
  const t = useSampleT()
  const run = findSample(index, params.sid ?? '')
  const back = (
    <Button variant="ghost" size="sm" nativeButton={false} render={<a href={`${LAB_PREFIX}/`} onClick={(e) => onLabLinkClick(e, `${LAB_PREFIX}/`)} />}>
      <Icon icon="chev.left" data-icon="inline-start" />
      {t('lab.public.sample.back')}
    </Button>
  )
  if (!run) {
    return <LabEmpty title={t('lab.public.sample.unknownTitle')} description={t('lab.public.sample.unknownHint', { id: params.sid ?? '' })} action={back} />
  }
  return <SampleRun key={`${index.version}/${run.runId}`} index={index} run={run} back={back} t={t} />
}

function SampleRun({ index, run, back, t }: { index: LabStaticIndex; run: LabStaticRun; back: React.ReactNode; t: SampleT }) {
  // 请求守卫先于本组件的数据请求安装（同一组件内的副作用按声明顺序执行）
  React.useEffect(() => installStaticGuard(), [])
  const vp = useViewport()
  const limit = maxConcurrentVideos(vp.cls, vp.soft)
  const group = React.useMemo(() => new VideoSyncGroup(), [])
  const { tape, tracks } = useSampleData(index, run)
  const [buffering, setBuffering] = React.useState(false)
  const [right, setRight] = React.useState<'views' | 'inspect'>('views')
  const [ctl, setCtl] = React.useState<InspectController | null>(null)
  const viewsRef = React.useRef<HTMLDivElement>(null)

  // 播放头初值与时长（磁带、轨迹、切片窗口；视频时长在加载后由同步器回写的 onLoadedMetadata 扩展）
  React.useEffect(() => {
    const t0 = typeof location === 'undefined' ? null : parseSampleT(location.search)
    usePlayhead.setState({ tS: t0 ?? 0, playing: false, rate: 1, durationS: Math.max(t0 ?? 0, sampleDurationS({ run })) })
    return () => usePlayhead.getState().pause()
  }, [run])
  React.useEffect(() => {
    const d = sampleDurationS({ tape: tape.kind === 'ready' ? tape.value : null, tracks: tracks.kind === 'ready' ? tracks.value : null, run })
    const st = usePlayhead.getState()
    if (d > st.durationS) st.setDuration(d)
  }, [tape, tracks, run])

  // 唯一的时钟循环：推进播放头（缓冲中、等待视频起播或 seek 完成时保持），再按播放头驱动全部视频
  React.useEffect(() => {
    let last = performance.now()
    let raf = 0
    let stale = false
    const step = (now: number): void => {
      // 不截短帧间隔（低帧率页面中截短会使播放头落后于视频时钟）；后台标签页恢复时的长间隔按 1 s 计
      const dt = Math.min(1, (now - last) / 1000)
      last = now
      const st = usePlayhead.getState()
      if (st.playing && group.canAdvance) st.advance(dt)
      const s2 = usePlayhead.getState()
      const b = group.tick({ tS: s2.tS, playing: s2.playing, rate: s2.rate }, now)
      if (b !== stale) {
        stale = b
        setBuffering(b)
      }
      raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [group])

  // 同步度量 → 数据属性（Playwright lab/public-samples 读取）；测试构建另挂 window.__awrSamples
  React.useEffect(() => {
    const h = setInterval(() => {
      const el = viewsRef.current
      if (!el) return
      const s = group.stats()
      el.dataset.syncPresented = String(s.presented)
      el.dataset.syncMaxDev = s.maxAbsDevFrames === null ? '' : String(s.maxAbsDevFrames)
      el.dataset.syncReseeks = String(s.reseeks)
      el.dataset.syncVideos = String(s.videos.length)
      el.dataset.guardBlocked = String(blockedRequests())
      el.dataset.syncHist = Object.entries(s.hist).map(([k, n]) => `${k}:${n}`).join(',')
    }, STATS_MS)
    const g = globalThis as { __awrSamples?: { group: VideoSyncGroup } | null }
    if (TEST_SWITCHES) g.__awrSamples = { group }
    return () => {
      clearInterval(h)
      if (TEST_SWITCHES && g.__awrSamples?.group === group) g.__awrSamples = null
    }
  }, [group])

  const source = React.useMemo(() => inspectSource(index, run), [index, run])
  const canInspect = index.features.inspect && source !== null
  const panel = right === 'inspect' && canInspect && source

  return (
    <section data-view="lab-public-run" data-sample={run.runId} className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          {back}
          <h1 className="text-ed-title font-bold">{t('lab.public.sample.title', { id: run.runId })}</h1>
          <Badge variant="outline" data-sample-notice="">{t('lab.public.run.notice')}</Badge>
        </div>
        <p className="text-sm text-muted-foreground" data-sample-meta="">
          {t('lab.public.run.meta', { event: run.eventId, version: run.eventVersion, world: run.worldId ?? '—', policy: run.policy || '—', n: run.nUav, obs: run.obsLevel || '—' })}
        </p>
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <LiveScene tape={tape} tracks={tracks} t={t} />
        <div ref={viewsRef} className="flex min-w-0 flex-col gap-2" data-sample-right={right} data-sync-buffering={buffering || undefined}>
          {canInspect ? (
            <div className="flex justify-end">
              <Button size="sm" variant="outline" onClick={() => setRight(right === 'inspect' ? 'views' : 'inspect')} data-sample-inspect-toggle="">
                <Icon icon={right === 'inspect' ? 'sensor.video' : 'layer.pointcloud'} data-icon="inline-start" />
                {t(right === 'inspect' ? 'lab.public.run.inspect.close' : 'lab.public.run.inspect.open')}
              </Button>
            </div>
          ) : null}
          {panel ? (
            <InspectBlock source={panel} probe={vp.probe} onController={setCtl} t={t} index={index} run={run} />
          ) : (
            <PrivateViews index={index} run={run} group={group} limit={limit} t={t} />
          )}
        </div>
      </div>
      <Timeline ctl={panel ? ctl : null} buffering={buffering} t={t} />
      <SampleFiles index={index} run={run} t={t} />
    </section>
  )
}

/** 事件回放读取播放头（每个动画帧的平面图重绘只发生在本组件，不牵动整页） */
function LiveScene({ tape, tracks, t }: Pick<ScenePlanProps, 'tape' | 'tracks' | 't'>) {
  const tS = usePlayhead((s) => s.tS)
  return <ScenePlan tape={tape} tracks={tracks} tS={tS} offsetS={SAMPLE_EVENT_OFFSET_S} t={t} />
}

/** 数据检视面板（右栏）：Tier B/A 加载 Viewer；Tier S、手机与平板为切片下载（面板 UNSUPPORTED 态的替代操作） */
function InspectBlock({ source, probe, onController, t, index, run }: {
  source: StaticSource; probe: DeviceProbe; onController: (c: InspectController | null) => void; t: SampleT; index: LabStaticIndex; run: LabStaticRun
}) {
  const it = useInspectT()
  const support = React.useMemo<Support>(() => {
    const q = new URLSearchParams(typeof location === 'undefined' ? '' : location.search)
    if (TEST_SWITCHES && q.get('inspect') === 'force') return { ok: true }
    return decideSupport(probe)
  }, [probe])
  return (
    <div className="flex min-h-[420px] flex-col rounded-md border" data-sample-inspect="">
      <InspectPanel source={source} playhead={labPlayheadPort} support={support} t={it} onController={onController}
        fallback={<SliceLinks index={index} run={run} t={t} it={it} />} />
      <p className="border-t px-3 py-1.5 text-hud-sub text-muted-foreground">{it('lab.inspect.note.static', { version: RERUN_VIEWER_VERSION })}</p>
    </div>
  )
}

/** 切片与底图的下载链接（Tier S 的数据检视替代） */
function SliceLinks({ index, run, t, it }: { index: LabStaticIndex; run: LabStaticRun; t: SampleT; it: (k: string) => string }) {
  return (
    <div className="flex flex-col items-center gap-2" data-sample-slices="">
      {run.slices.map((s) => {
        const hosted = isHosted(s)
        const label = t('lab.public.run.slices.item', { event: s.eventId, from: s.tFromS.toFixed(1), to: s.tToS.toFixed(1) })
        const href = artifactSrc(index, s)
        return href ? (
          <Button key={s.path} size="sm" variant="outline" nativeButton={false}
            render={<a href={href} {...(hosted ? { download: '' } : { target: '_blank', rel: 'noreferrer noopener' })} data-sample-slice={s.path} />}>
            <Icon icon="data.export" data-icon="inline-start" />
            {`${it('lab.inspect.download')} · ${label}`}
          </Button>
        ) : null
      })}
      {run.basemap ? (
        <Button size="sm" variant="ghost" nativeButton={false} render={run.basemapExternal
          ? <a href={run.basemapExternal} target="_blank" rel="noreferrer noopener" data-sample-basemap={run.basemap} />
          : <a href={index.url(run.basemap)} download="" data-sample-basemap={run.basemap} />}>
          <Icon icon="data.export" data-icon="inline-start" />
          basemap.rrd
        </Button>
      ) : null}
      <p className="max-w-md text-center text-hud-sub text-muted-foreground">{t('lab.public.run.slices.hint')}</p>
    </div>
  )
}

/** lab 时间线：播放、步进 0.1 s、拖动（拖动起止转给数据检视同步器）、速率；缓冲中提示 */
function Timeline({ ctl, buffering, t }: { ctl: InspectController | null; buffering: boolean; t: SampleT }) {
  const ph = usePlayhead()
  const scrub = React.useRef(false)
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border px-3 py-2" data-sample-timeline="" data-playing={ph.playing || undefined}>
      <Button size="icon" variant="outline" className="size-8 pointer-coarse:size-11" aria-label={t(ph.playing ? 'lab.public.run.tl.pause' : 'lab.public.run.tl.play')}
        onClick={() => (ph.playing ? ph.pause() : ph.play())} data-sample-play="">
        <Icon icon={ph.playing ? 'tl.pause' : 'tl.play'} />
      </Button>
      <Button size="icon" variant="ghost" className="size-8 pointer-coarse:size-11" aria-label={t('lab.public.run.tl.stepBack')} onClick={() => ph.seek(ph.tS - 0.1)}>
        <Icon icon="tl.stepback" />
      </Button>
      <Button size="icon" variant="ghost" className="size-8 pointer-coarse:size-11" aria-label={t('lab.public.run.tl.stepFwd')} onClick={() => ph.seek(ph.tS + 0.1)}>
        <Icon icon="tl.stepfwd" />
      </Button>
      <Slider className="min-w-40 flex-1" min={0} max={Math.max(ph.durationS, 0.1)} step={0.1} value={[ph.tS]} aria-label={t('lab.public.run.tl.aria')}
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
      <span className="font-mono text-hud-sub tabular-nums" data-sample-clock={ph.tS.toFixed(3)}>{`T+${clockText(ph.tS)} / ${clockText(ph.durationS)}`}</span>
      <ToggleGroup spacing={0} size="sm" variant="outline" value={[String(ph.rate)]} aria-label={t('lab.public.run.tl.rate')}
        onValueChange={(v: unknown[]) => {
          const r = Number(v[0])
          if ((SAMPLE_RATES as readonly number[]).includes(r)) ph.setRate(r)
        }}>
        {SAMPLE_RATES.map((r) => <ToggleGroupItem key={r} value={String(r)} className="font-mono tabular-nums pointer-coarse:h-11 pointer-coarse:min-w-11">{`${r}×`}</ToggleGroupItem>)}
      </ToggleGroup>
      {buffering ? (
        <span className="inline-flex items-center gap-1 text-hud-sub text-muted-foreground" data-sample-buffering="">
          <Icon icon="layer.loading" />
          {t('lab.public.run.tl.buffering')}
        </span>
      ) : null}
    </div>
  )
}

/** 样例数据下载：视频、切片与底图；有分发渠道副本或 > 64 MB 的指向分发渠道（AWR-14 §21.14.2 规则 2；ADR-221） */
function SampleFiles({ index, run, t }: { index: LabStaticIndex; run: LabStaticRun; t: SampleT }) {
  type Row = { kind: 'video' | 'slice' | 'basemap'; path: string; label: string; bytes: number | null; hosted: boolean; external: string | null }
  const rows: Row[] = [
    ...run.videos.map((v) => ({ kind: 'video' as const, path: v.path, label: `${v.uav} · ${v.cam}`, bytes: v.bytes, external: v.external,
      hosted: isHosted(v) })),
    ...run.slices.map((s) => ({ kind: 'slice' as const, path: s.path, bytes: s.bytes, external: s.external, hosted: isHosted(s),
      label: t('lab.public.run.slices.item', { event: s.eventId, from: s.tFromS.toFixed(1), to: s.tToS.toFixed(1) }) })),
    ...(run.basemap ? [{ kind: 'basemap' as const, path: run.basemap, label: 'basemap.rrd', bytes: run.basemapBytes, external: run.basemapExternal,
      hosted: isHosted({ bytes: run.basemapBytes, external: run.basemapExternal }) }] : []),
  ]
  if (!rows.length) return null
  const cols: LfColumn<Row>[] = [
    { key: 'kind', label: t('lab.public.run.files.col.kind'), format: (f) => t(`lab.public.run.files.kind.${f.kind}`) },
    { key: 'file', label: t('lab.public.run.files.col.file'), format: (f) => <span className="font-mono">{f.label}</span> },
    { key: 'size', label: t('lab.public.run.files.col.size'), unit: 'MB', align: 'right', format: (f) => <span className="font-mono">{fmtMb(f.bytes)}</span> },
    {
      key: 'get', label: '', align: 'right',
      format: (f) => (f.hosted ? (
        <Button variant="ghost" size="sm" nativeButton={false} render={<a href={index.url(f.path)} download="" data-sample-file={f.path} />}>
          <Icon icon="data.export" data-icon="inline-start" />
          {t('lab.public.run.files.download')}
        </Button>
      ) : f.external ? (
        <Button variant="ghost" size="sm" nativeButton={false} render={<a href={f.external} target="_blank" rel="noreferrer noopener" data-sample-file-external={f.path} />}>
          {t('lab.public.run.files.offsite')}
          <Icon icon="external" data-icon="inline-end" />
        </Button>
      ) : <span className="text-muted-foreground">{t('lab.public.run.files.offsite')}</span>),
    },
  ]
  return (
    <div className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold">{t('lab.public.run.files.title')}</h2>
      <div className="overflow-x-auto">
        <LfTable columns={cols} rows={rows} rowKey={(f) => f.path} ariaLabel={t('lab.public.run.files.aria')} figureId="lab-public-run-files"
          height={tableMaxHeight(rows.length)} />
      </div>
    </div>
  )
}
