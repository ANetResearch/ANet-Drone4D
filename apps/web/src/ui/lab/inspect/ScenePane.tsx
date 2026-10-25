// 数据检视页左栏：场景总览（AWR-14 §21.9.1 左侧"机队总览与真值实体；研究者为观测层，评测员可切换真值"）。北向上的平面图：机队按运行
// 清单的航迹在播放头时刻的位置（观测层）；评测 catalog 会话另画磁带中的脚本演员（真值层，浏览器端解码，与观测墙的事件平面图同一算法，
// ui/lab/wall/eventPlan.ts）。点击机体或演员即选中并经 Viewer URL 片段定位（§21.9.3 第 3 条）；Viewer 中的选择以描边高亮回显。
// 只读播放头时刻（lab 播放头为唯一时间来源）；不使用红色（AWR-15，选中以前景色描边表示）。
import * as React from 'react'
import type { TapeReader } from '@/engine'
import { actorTrails, placeLabels, planFrame, scaleBar, toPx, uavXY, type UavPath } from '@/ui/lab/wall/eventPlan'
import type { InspectT } from './InspectPanel'
import type { LabSelection } from './rerunSync'

export interface SceneFleet { uav: string; path: UavPath }
export interface SceneTape { r: TapeReader; offNs: number }

const PX = 360

export function ScenePane({ fleet, tape, tS, durS, selection, onSelect, t }: {
  fleet: readonly SceneFleet[]; tape: SceneTape | null; tS: number; durS: number; selection: LabSelection | null
  onSelect: (s: LabSelection) => void; t: InspectT
}) {
  const r = tape?.r ?? null
  const offS = (tape?.offNs ?? 0) / 1e9
  const trails = React.useMemo(() => (r ? actorTrails(r, 1, offS, offS + Math.max(durS, 1)) : []), [r, offS, durS])
  const tracks = React.useMemo(() => {
    const n = 120
    const span = Math.max(durS, 1)
    return fleet.map((u) => ({ uav: u.uav, pts: Array.from({ length: n + 1 }, (_, i) => uavXY(u.path, (i / n) * span)) }))
  }, [fleet, durS])
  const frame = React.useMemo(() => planFrame([...trails.flatMap((a) => a.segs.flat()), ...tracks.flatMap((u) => u.pts)]), [trails, tracks])
  if (!fleet.length && !r) {
    return <p className="p-3 text-hud-sub text-muted-foreground" data-scene-empty="">{t('lab.inspect.scene.empty')}</p>
  }
  const P = (x: number, y: number) => toPx(frame, PX, x, y)
  const line = (pts: [number, number][]) => pts.map(([x, y]) => P(x, y).map((v) => v.toFixed(1)).join(',')).join(' ')
  const st = r ? r.actorStates((tape?.offNs ?? 0) + Math.round(tS * 1e9), Array.from({ length: r.nScripted }, (_, i) => i)) : null
  const shown = st ? Array.from(st.ids, (_i, j) => j).filter((j) => st.state[j] !== 0 && st.state[j] !== 5) : []
  const uavPx = fleet.map((u) => P(...uavXY(u.path, tS)))
  const actPx = st ? Array.from(st.ids, (_i, j) => P(st.pos[3 * j]!, st.pos[3 * j + 1]!)) : []
  const labelY = placeLabels([
    ...shown.map((j) => ({ x: actPx[j]![0] + 6, y: actPx[j]![1] - 5, w: 6 * r!.header.actors[st!.ids[j]!]!.id.length })),
    ...fleet.map((u, k) => ({ x: uavPx[k]![0] + 7, y: uavPx[k]![1] + 3, w: 6 * u.uav.length })),
  ])
  const bar = scaleBar(frame.size)
  const isSel = (kind: LabSelection['kind'], id: string) => selection?.kind === kind && selection.id === id
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 p-3" data-figure="lab-inspect-scene">
      <h2 className="text-ed-sub font-bold">{t('lab.inspect.scene.title')}</h2>
      <svg viewBox={`0 0 ${PX} ${PX}`} role="img" aria-label={t('lab.inspect.scene.aria')} className="aspect-square w-full max-w-[560px] rounded-sm border"
        style={{ background: 'var(--lf-bg)' }}>
        {tracks.map((u) => (
          <polyline key={u.uav} points={line(u.pts)} fill="none" strokeWidth={1} strokeDasharray="3 3" style={{ stroke: 'var(--lf-grid)' }} />
        ))}
        {trails.map((a) => a.segs.map((s, k) => (
          <polyline key={`${a.id}/${k}`} points={line(s)} fill="none" strokeWidth={1} style={{ stroke: 'var(--lf-faint)' }} />
        )))}
        {fleet.map((u, k) => {
          const [x, y] = uavPx[k]!
          const sel = isSel('uav', u.uav)
          return (
            <g key={u.uav} data-scene-uav={u.uav} data-selected={sel ? '' : undefined} className="cursor-pointer" onClick={() => onSelect({ kind: 'uav', id: u.uav })}>
              <path d={`M${x},${y - 6} L${x + 6},${y} L${x},${y + 6} L${x - 6},${y} Z`} strokeWidth={sel ? 2 : 0}
                style={{ fill: 'var(--lf-data2)', stroke: 'var(--lf-txt)' }} />
              <text x={x + 8} y={labelY[shown.length + k]} fontSize={10} style={{ fill: sel ? 'var(--lf-txt)' : 'var(--lf-mut)' }}>{u.uav}</text>
            </g>
          )
        })}
        {st && r ? shown.map((j, n) => {
          const id = r.header.actors[st.ids[j]!]!.id
          const s = st.state[j]!
          const [x, y] = actPx[j]!
          const vis = s === 1 || s === 2
          const sel = isSel('actor', id)
          return (
            <g key={id} data-scene-actor={id} data-selected={sel ? '' : undefined} className="cursor-pointer" onClick={() => onSelect({ kind: 'actor', id })}>
              <circle cx={x} cy={y} r={vis ? 4.5 : 4} strokeWidth={sel ? 2.5 : 1.5}
                style={vis ? { fill: 'var(--lf-data)', stroke: sel ? 'var(--lf-txt)' : 'var(--lf-bg)' } : { fill: 'none', stroke: sel ? 'var(--lf-txt)' : 'var(--lf-data)' }} />
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
      <p className="text-hud-sub text-muted-foreground" data-scene-selection={selection ? `${selection.kind}:${selection.id}` : ''}>
        {selection
          ? t(selection.kind === 'uav' ? 'lab.inspect.scene.selectedUav' : 'lab.inspect.scene.selectedActor', { id: selection.id })
          : t('lab.inspect.scene.hint')}
      </p>
    </div>
  )
}
