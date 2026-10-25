// 样例运行页的事件回放（AWR-14 §21.29.3 左栏；§21.13 第 4 条；D3-AC-21"事件回放由前端磁带解码器驱动（只显示脚本演员）"）。Owner: M15.
// 北向上的平面图：脚本演员取事件磁带在播放头时刻的插值位置（Hermite，状态或容器变化处阶跃；与观测墙事件平面图同一算法，
// ui/lab/wall/eventPlan.ts），淡色折线为演员全程轨迹；机队取观测层轨迹 tracks_obs.bin（带噪导航解）在播放头时刻的位置，虚线为全程
// 航迹。只读播放头时刻；不显示真值叠加与背景人群（背景人群只在视角视频中）；不使用红色（AWR-15）。
import * as React from 'react'
import type { TapeReader } from '@/engine/actors/tape/decode'
import { actorTrails, placeLabels, planFrame, scaleBar, toPx } from '@/ui/lab/wall/eventPlan'
import type { Loadable } from './sampleData'
import type { SampleT } from './text'
import { trackPolyline, trackXY, type FleetTracks } from './tracks'

const PX = 360
const VISIBLE = (s: number): boolean => s === 1 || s === 2

export interface ScenePlanProps {
  tape: Loadable<TapeReader>
  tracks: Loadable<FleetTracks>
  /** 播放头（运行仿真秒） */
  tS: number
  /** 事件偏移（s）：事件时刻 = tS + offsetS */
  offsetS: number
  t: SampleT
}

export function ScenePlan({ tape, tracks, tS, offsetS, t }: ScenePlanProps) {
  const r = tape.kind === 'ready' ? tape.value : null
  const f = tracks.kind === 'ready' ? tracks.value : null
  const trails = React.useMemo(() => (r ? actorTrails(r, 1) : []), [r])
  const routes = React.useMemo(() => (f ? f.uavs.map((u) => ({ uav: u.uavId, pts: trackPolyline(f, u, 1) })) : []), [f])
  const frame = React.useMemo(() => planFrame([...trails.flatMap((a) => a.segs.flat()), ...routes.flatMap((u) => u.pts)]), [trails, routes])
  const loading = tape.kind === 'loading' || tracks.kind === 'loading'

  const notes: string[] = []
  if (tape.kind === 'error') notes.push(t('lab.public.run.scene.noTape', { code: tape.code }))
  if (tracks.kind === 'error') notes.push(t('lab.public.run.scene.noTracks'))

  const header = (
    <div className="flex items-baseline justify-between gap-2">
      <h2 className="text-sm font-semibold">{t('lab.public.run.scene.title')}</h2>
      {r ? <span className="font-mono text-hud-sub text-muted-foreground">{`${r.header.event_id} v${r.header.event_version}`}</span> : null}
    </div>
  )
  if (!r && !f) {
    return (
      <section className="flex min-w-0 flex-col gap-2" data-figure="lab-public-scene" data-scene-state={loading ? 'loading' : 'empty'}>
        {header}
        <p className="text-hud-sub text-muted-foreground">
          {loading ? t('lab.public.run.scene.loading') : notes.length ? notes.join(' ') : t('lab.public.run.scene.empty')}
        </p>
      </section>
    )
  }

  const P = (x: number, y: number) => toPx(frame, PX, x, y)
  const line = (pts: readonly [number, number][]) => pts.map(([x, y]) => P(x, y).map((v) => v.toFixed(1)).join(',')).join(' ')
  const tEvNs = Math.round((tS + offsetS) * 1e9)
  const st = r ? r.actorStates(tEvNs, Array.from({ length: r.nScripted }, (_, i) => i)) : null
  const shown = st ? Array.from(st.ids, (_i, j) => j).filter((j) => st.state[j] !== 0 && st.state[j] !== 5) : []
  const actPx = st ? Array.from(st.ids, (_i, j) => P(st.pos[3 * j]!, st.pos[3 * j + 1]!)) : []
  const fleet = f ? f.uavs.map((u) => ({ uav: u.uavId, xy: trackXY(f, u, tS) })).filter((u): u is { uav: string; xy: [number, number] } => u.xy !== null) : []
  const uavPx = fleet.map((u) => P(...u.xy))
  const labelY = placeLabels([
    ...shown.map((j) => ({ x: actPx[j]![0] + 6, y: actPx[j]![1] - 5, w: 6 * r!.header.actors[st!.ids[j]!]!.id.length })),
    ...fleet.map((u, k) => ({ x: uavPx[k]![0] + 7, y: uavPx[k]![1] + 3, w: 6 * u.uav.length })),
  ])
  const bar = scaleBar(frame.size)

  return (
    <section className="flex min-w-0 flex-col gap-2" data-figure="lab-public-scene" data-scene-state="ready" data-scene-actors={shown.length}
      data-scene-uavs={fleet.length}>
      {header}
      <svg viewBox={`0 0 ${PX} ${PX}`} role="img" aria-label={t('lab.public.run.scene.aria')} className="lf-svg aspect-square w-full rounded-sm border"
        style={{ background: 'var(--lf-bg)' }}>
        {routes.map((u) => (
          <polyline key={u.uav} points={line(u.pts)} fill="none" strokeWidth={1} strokeDasharray="3 3" style={{ stroke: 'var(--lf-grid)' }} />
        ))}
        {trails.map((a) => a.segs.map((s, k) => (
          <polyline key={`${a.id}/${k}`} points={line(s)} fill="none" strokeWidth={1} style={{ stroke: 'var(--lf-faint)' }} />
        )))}
        {fleet.map((u, k) => {
          const [x, y] = uavPx[k]!
          return (
            <g key={u.uav} data-scene-uav={u.uav}>
              <path d={`M${x},${y - 6} L${x + 6},${y} L${x},${y + 6} L${x - 6},${y} Z`} style={{ fill: 'var(--lf-data2)' }} />
              <text x={x + 8} y={labelY[shown.length + k]} fontSize={10} style={{ fill: 'var(--lf-mut)' }}>{u.uav}</text>
            </g>
          )
        })}
        {st && r ? shown.map((j, n) => {
          const id = r.header.actors[st.ids[j]!]!.id
          const s = st.state[j]!
          const [x, y] = actPx[j]!
          const vis = VISIBLE(s)
          return (
            <g key={id} data-scene-actor={id} data-state={s}>
              <circle cx={x} cy={y} r={vis ? 4.5 : 4} strokeWidth={1.5}
                style={vis ? { fill: 'var(--lf-data)', stroke: 'var(--lf-bg)' } : { fill: 'none', stroke: 'var(--lf-data)' }} />
              <text x={x + 6} y={labelY[n]} fontSize={10} style={{ fill: 'var(--lf-txt)' }}>{id}</text>
            </g>
          )
        }) : null}
        <g transform={`translate(10 ${PX - 12})`}>
          <line x1={0} x2={(bar / frame.size) * PX} y1={0} y2={0} strokeWidth={2} style={{ stroke: 'var(--lf-mut)' }} />
          <text x={0} y={-4} fontSize={10} style={{ fill: 'var(--lf-mut)' }}>{`${bar} m`}</text>
        </g>
        <text x={PX - 10} y={16} fontSize={10} textAnchor="end" style={{ fill: 'var(--lf-mut)' }}>N</text>
      </svg>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-hud-sub text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <svg width={10} height={10} aria-hidden="true"><circle cx={5} cy={5} r={4} style={{ fill: 'var(--lf-data)' }} /></svg>
          {t('lab.public.run.scene.legendActor')}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width={10} height={10} aria-hidden="true"><circle cx={5} cy={5} r={3.5} fill="none" strokeWidth={1.5} style={{ stroke: 'var(--lf-data)' }} /></svg>
          {t('lab.public.run.scene.legendInside')}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width={12} height={12} aria-hidden="true"><path d="M6,0 L12,6 L6,12 L0,6 Z" style={{ fill: 'var(--lf-data2)' }} /></svg>
          {t('lab.public.run.scene.legendUav')}
        </span>
        {r ? <span className="ml-auto font-mono tabular-nums" data-scene-clock="">{t('lab.public.run.scene.clock', { t: (tEvNs / 1e9).toFixed(1) })}</span> : null}
      </div>
      {notes.length ? <p className="text-hud-sub text-muted-foreground">{notes.join(' ')}</p> : null}
      <p className="text-hud-sub text-muted-foreground">{t('lab.public.run.scene.hint')}</p>
    </section>
  )
}
