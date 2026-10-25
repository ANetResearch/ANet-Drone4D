// 样例运行页的多机私有视角（AWR-14 §21.29.3 右栏、§21.7.4 公开站行、§21.14.3；D3-AC-21"RGB 与热成像伪彩视频按仿真时间同步，
// 偏差 ≤ 1 帧"；UX-AC-113）。Owner: M15.
// - 每机 RGB 与热成像伪彩视频（runs/<rid>/video/<uav>_<cam>.mp4，640 × 360 @ 10 Hz，H.264），只显示该机实际收到的观测，无真值叠加；
// - 视图方式：RGB、热成像、并排（并排时 RGB 一行、热成像一行，§21.29.3 线框）；同时播放路数按设备档限制（videoSync.ts
//   maxConcurrentVideos），超过上限时不提供并排；手机只显示一机，以"上一架、下一架"切换；
// - 时间：<video> 只由 lab 播放头驱动（VideoSyncGroup，页面每个动画帧 tick 一次），格内不保留自己的时钟；每次合成新帧时把
//   mediaTime 交给同步器记录偏差；
// - 视频地址：分发渠道副本优先（ADR-221：emax 出口约 3 Mbps，视频经 ModelScope 播放），以 CORS 模式请求（/lab/ 为 COEP
//   require-corp）；没有副本时取本站版本目录；单文件 > 64 MB 且没有副本的不发起请求，格内显示说明（ADR-201）；
// - 热成像为白热调色板的伪彩视频，附色标（不以红色表达高温，AWR-14 §21.7.3 热成像行）。
import * as React from 'react'
import { Button } from '@/ui/components/ui/button'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { usePlayhead } from '@/stores/lab/playhead'
import { artifactSrc, type LabStaticIndex, type LabStaticRun, type LabStaticVideo } from '../staticIndex'
import type { SampleT } from './text'
import {
  availableModes, defaultMode, frameCount, groupByUav, targetFrame, VIDEO_FPS, visibleTiles, type Tile, type VideoSyncGroup,
  type ViewMode,
} from './videoSync'

/** 视频首帧的运行仿真时刻（s）：样例运行的视频自运行起点编码（Env 运行，M27-FR-135） */
export const VIDEO_T0_S = 0

export interface PrivateViewsProps {
  index: LabStaticIndex
  run: LabStaticRun
  group: VideoSyncGroup
  /** 同时播放的视频路数上限 */
  limit: number
  t: SampleT
}

export const tileId = (v: { uav: string; cam: string }): string => `${v.uav}/${v.cam}`

export function PrivateViews({ index, run, group, limit, t }: PrivateViewsProps) {
  const groups = React.useMemo(() => groupByUav(run.videos), [run.videos])
  const modes = React.useMemo(() => availableModes(groups, limit), [groups, limit])
  const [chosen, setChosen] = React.useState<ViewMode | null>(null)
  const mode = chosen && modes.includes(chosen) ? chosen : defaultMode(modes)
  const [focus, setFocus] = React.useState<string | null>(null)
  const tiles = mode ? visibleTiles(groups, mode, limit, focus) : []
  const single = limit === 1
  const cols = single ? 1 : Math.max(1, Math.min(3, new Set(tiles.map((x) => x.uav)).size))
  const step = (d: number): void => {
    const i = Math.max(0, groups.findIndex((g) => g.uav === (focus ?? groups[0]?.uav)))
    setFocus(groups[(i + d + groups.length) % groups.length]?.uav ?? null)
  }

  return (
    <section className="flex min-w-0 flex-col gap-2" data-figure="lab-public-views" data-view-mode={mode ?? ''} data-view-limit={limit}>
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{t('lab.public.run.views.title')}</h2>
        {modes.length > 1 ? (
          <ToggleGroup spacing={0} size="sm" variant="outline" value={mode ? [mode] : []} aria-label={t('lab.public.run.views.modeAria')}
            onValueChange={(v: unknown[]) => {
              const m = v[0]
              if (m === 'rgb' || m === 'thermal' || m === 'both') setChosen(m)
            }} className="ml-auto" data-view-modes="">
            {modes.map((m) => <ToggleGroupItem key={m} value={m} data-view-mode-item={m}>{t(`lab.public.run.views.mode.${m}`)}</ToggleGroupItem>)}
          </ToggleGroup>
        ) : null}
      </div>
      {!tiles.length ? (
        <p className="text-hud-sub text-muted-foreground">{t('lab.public.run.views.empty')}</p>
      ) : (
        <>
          {single && groups.length > 1 ? (
            <div className="flex items-center gap-2">
              <Button size="icon" variant="outline" className="size-11" aria-label={t('lab.public.run.views.prevUav')} onClick={() => step(-1)}>
                <Icon icon="chev.left" />
              </Button>
              <span className="flex-1 text-center font-mono text-sm">{tiles[0]?.uav}</span>
              <Button size="icon" variant="outline" className="size-11" aria-label={t('lab.public.run.views.nextUav')} onClick={() => step(1)}>
                <Icon icon="chev.right" />
              </Button>
            </div>
          ) : null}
          <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
            {tiles.map((x) => <VideoTile key={`${x.kind}/${tileId(x.video)}`} tile={x} index={index} group={group} t={t} />)}
          </div>
          {tiles.some((x) => x.kind === 'thermal') ? <ThermalScale t={t} /> : null}
        </>
      )}
      <p className="text-hud-sub text-muted-foreground">
        {t('lab.public.run.views.hint')}
        {groups.length * 2 > limit ? ` ${t('lab.public.run.views.limit', { n: limit })}` : ''}
      </p>
    </section>
  )
}

type TileState = 'loading' | 'ready' | 'error'

function VideoTile({ tile, index, group, t }: { tile: Tile<LabStaticVideo>; index: LabStaticIndex; group: VideoSyncGroup; t: SampleT }) {
  const { video, kind, uav } = tile
  const src = artifactSrc(index, video)
  const playable = src !== null
  const id = tileId(video)
  const ref = React.useRef<HTMLVideoElement>(null)
  const [state, setState] = React.useState<TileState>('loading')
  const [durationS, setDurationS] = React.useState<number>(Number.NaN)
  const tm = React.useMemo(() => ({ t0S: VIDEO_T0_S, fps: VIDEO_FPS }), [])
  // 只在应显示帧变化时重渲染（10 Hz），不随播放头每个动画帧重渲染
  const k = usePlayhead((s) => targetFrame(s.tS, tm))

  React.useEffect(() => {
    const el = ref.current
    if (!el || !playable) return
    const off = group.add(id, el, tm)
    let h = 0
    const onFrame: VideoFrameRequestCallback = (_now, meta) => {
      group.presentedAt(id, meta.mediaTime, meta.expectedDisplayTime)
      h = el.requestVideoFrameCallback(onFrame)
    }
    if (typeof el.requestVideoFrameCallback === 'function') h = el.requestVideoFrameCallback(onFrame)
    return () => {
      off()
      if (h && typeof el.cancelVideoFrameCallback === 'function') el.cancelVideoFrameCallback(h)
    }
  }, [group, id, tm, playable])

  const n = frameCount(durationS, tm.fps)
  const outside = n !== null && (k < 0 || k >= n)
  const label = t(`lab.public.run.kind.${kind}`)
  return (
    <figure data-view-tile={`${kind}/${id}`} data-view-kind={kind} data-view-state={playable ? state : 'offsite'} data-view-outside={outside || undefined}
      className="relative m-0 aspect-video min-w-0 overflow-hidden rounded-md border bg-muted">
      {playable ? (
        <video ref={ref} src={src} crossOrigin={video.external ? 'anonymous' : undefined} muted playsInline preload="auto" disablePictureInPicture disableRemotePlayback
          aria-label={t('lab.public.run.views.tileAria', { uav, kind: label })} className={outside ? 'size-full object-contain opacity-60' : 'size-full object-contain'}
          onLoadedMetadata={(e) => {
            const d = e.currentTarget.duration
            setDurationS(d)
            // 视频覆盖区间扩展播放头时长（磁带与轨迹缺失时仍可完整播放）
            const st = usePlayhead.getState()
            if (Number.isFinite(d) && tm.t0S + d > st.durationS) st.setDuration(tm.t0S + d)
          }} onLoadedData={() => setState('ready')} onError={() => setState('error')} />
      ) : (
        <div className="flex size-full flex-col items-center justify-center gap-2 p-3 text-center text-hud-sub text-muted-foreground">
          <span>{t('lab.public.run.views.offsite')}</span>
          {video.external ? (
            <Button size="sm" variant="outline" nativeButton={false} render={<a href={video.external} target="_blank" rel="noreferrer noopener" />}>
              {t('lab.public.run.views.openExternal')}
              <Icon icon="external" data-icon="inline-end" />
            </Button>
          ) : null}
        </div>
      )}
      {playable && state !== 'ready' ? (
        <span className="absolute inset-0 flex items-center justify-center text-hud-sub text-muted-foreground">
          {t(state === 'error' ? 'lab.public.run.views.error' : 'lab.public.run.views.loading')}
        </span>
      ) : null}
      {playable && state === 'ready' && outside ? (
        <span className="absolute right-1 top-1 inline-flex items-center gap-1 rounded-sm bg-background/80 px-1.5 text-hud-sub">
          <Icon icon="state.stale" />
          {t('lab.public.run.views.outside')}
        </span>
      ) : null}
      <figcaption className="absolute inset-x-0 bottom-0 flex items-center gap-2 bg-background/70 px-2 py-0.5 text-hud-sub">
        <span className="font-mono">{uav}</span>
        <span className="text-muted-foreground">{label}</span>
        <span className="ml-auto font-mono tabular-nums">{n !== null && !outside ? `#${k}` : '—'}</span>
      </figcaption>
    </figure>
  )
}

/** 白热色标：低温深、高温浅（AWR-14 §21.7.3；预渲染视频的调色板） */
function ThermalScale({ t }: { t: SampleT }) {
  return (
    <div className="flex items-center gap-2 text-hud-sub text-muted-foreground" data-thermal-scale="white-hot">
      <span>{t('lab.public.run.thermal.scale')}</span>
      <span>{t('lab.public.run.thermal.low')}</span>
      <span aria-hidden="true" className="h-2 w-32 rounded-sm border" style={{ background: 'linear-gradient(to right, var(--g950), var(--white))' }} />
      <span>{t('lab.public.run.thermal.high')}</span>
    </div>
  )
}
