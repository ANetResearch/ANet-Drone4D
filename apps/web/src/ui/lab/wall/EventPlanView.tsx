// Event plan panel (P0a-2 event replay; evaluation view only, the tape is event truth, M27 §7.9). The run's tape is fetched
// once (BN truth/event/tape), decoded in the browser (engine/actors/tape) and drawn as a north-up plan: scripted actor
// trails (faint), actor positions at event time offset + playhead (data role; hollow when inside a vehicle or building),
// UAV ground tracks and positions from the run manifest (second data role). No red (AWR-15).
import { useQuery } from '@tanstack/react-query'
import * as React from 'react'
import { useT } from '@/app/i18n'
import { TapeReader } from '@/engine'
import { benchBytes } from '@/net/bench'
import { usePlayhead } from '@/stores/lab/playhead'
import { actorTrails, placeLabels, planFrame, scaleBar, toPx, uavXY, type UavPath } from './eventPlan'

export interface RunEventMeta { event_id: string; version: number; offset_s: number }
export interface FleetEntry { uav: string; path: UavPath }

const PX = 288
const STATES = ['absent', 'moving', 'waiting', 'inVehicle', 'inside', 'gone'] as const

export function EventPlan({ rid, event, fleet }: { rid: string; event: RunEventMeta; fleet: readonly FleetEntry[] }) {
  const t = useT()
  const tS = usePlayhead((s) => s.tS)
  const durS = usePlayhead((s) => s.durationS)
  const q = useQuery({
    queryKey: ['lab', 'tape', rid],
    queryFn: async ({ signal }) => {
      const b = await benchBytes(`/runs/${encodeURIComponent(rid)}/truth/event/tape`, signal)
      const off = Number(b.headers.get('X-Event-Offset-Ns') ?? Math.round(event.offset_s * 1e9))
      return { r: new TapeReader(b.buf), offNs: off }
    },
    staleTime: Infinity,
  })
  const r = q.data?.r
  const offS = (q.data?.offNs ?? 0) / 1e9
  // trails over the captured window only, so the plan frames what the UAVs saw
  const trails = React.useMemo(() => (r ? actorTrails(r, 1, offS, offS + Math.max(durS, 1)) : []), [r, offS, durS])
  const uavTracks = React.useMemo(() => {
    const n = 120
    const span = Math.max(durS, 1)
    return fleet.map((u) => ({ uav: u.uav, pts: Array.from({ length: n + 1 }, (_, i) => uavXY(u.path, (i / n) * span)) }))
  }, [fleet, durS])
  const frame = React.useMemo(
    () => planFrame([...trails.flatMap((a) => a.segs.flat()), ...uavTracks.flatMap((u) => u.pts)]),
    [trails, uavTracks],
  )
  const tEvNs = (q.data?.offNs ?? 0) + Math.round(tS * 1e9)
  const st = r ? r.actorStates(tEvNs, Array.from({ length: r.nScripted }, (_, i) => i)) : null
  const P = (x: number, y: number) => toPx(frame, PX, x, y)
  const bar = scaleBar(frame.size)
  const line = (pts: [number, number][]) => pts.map(([x, y]) => P(x, y).map((v) => v.toFixed(1)).join(',')).join(' ')
  const uavPx = fleet.map((u) => P(...uavXY(u.path, tS)))
  const actPx = st ? Array.from(st.ids, (_i, j) => P(st.pos[3 * j]!, st.pos[3 * j + 1]!)) : []
  const shownActors = st ? Array.from(st.ids, (_i, j) => j).filter((j) => st.state[j] !== 0 && st.state[j] !== 5) : []
  const labelY = placeLabels([
    ...shownActors.map((j) => ({ x: actPx[j]![0] + 6, y: actPx[j]![1] - 5, w: 6 * r!.header.actors[st!.ids[j]!]!.id.length })),
    ...fleet.map((u, k) => ({ x: uavPx[k]![0] + 7, y: uavPx[k]![1] + 3, w: 6 * u.uav.length })),
  ])

  return (
    <aside data-figure="lab-event-plan" className="flex w-80 shrink-0 flex-col gap-2 border-l p-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-ed-sub font-bold">{t('lab.event.plan')}</h2>
        <span className="font-mono text-hud-sub text-muted-foreground">{`${event.event_id} v${event.version}`}</span>
      </div>
      {q.isError ? (
        <p className="text-hud-sub text-muted-foreground">{t('lab.event.unavailable')}</p>
      ) : !r || !st ? (
        <p className="text-hud-sub text-muted-foreground">{t('lab.wall.loading')}</p>
      ) : (
        <>
          <svg width={PX} height={PX} viewBox={`0 0 ${PX} ${PX}`} role="img" aria-label={t('lab.event.planAria', { id: event.event_id })}
            className="rounded-sm border" style={{ background: 'var(--lf-bg)' }}>
            {uavTracks.map((u) => (
              <polyline key={u.uav} points={line(u.pts)} fill="none" strokeWidth={1} strokeDasharray="3 3" style={{ stroke: 'var(--lf-grid)' }} />
            ))}
            {trails.map((a) => a.segs.map((s, k) => (
              <polyline key={`${a.id}/${k}`} points={line(s)} fill="none" strokeWidth={1} style={{ stroke: 'var(--lf-faint)' }} />
            )))}
            {fleet.map((u, k) => {
              const [x, y] = uavPx[k]!
              return (
                <g key={u.uav} data-uav={u.uav}>
                  <path d={`M${x},${y - 5} L${x + 5},${y} L${x},${y + 5} L${x - 5},${y} Z`} style={{ fill: 'var(--lf-data2)' }} />
                  <text x={x + 7} y={labelY[shownActors.length + k]} fontSize={10} style={{ fill: 'var(--lf-mut)' }}>{u.uav}</text>
                </g>
              )
            })}
            {shownActors.map((j, n) => {
              const i = st.ids[j]!
              const s = st.state[j]!
              const [x, y] = actPx[j]!
              const vis = s === 1 || s === 2
              return (
                <g key={i} data-actor={r.header.actors[i]!.id} data-state={s}>
                  <circle cx={x} cy={y} r={vis ? 4 : 3.5} strokeWidth={1.5}
                    style={vis ? { fill: 'var(--lf-data)', stroke: 'var(--lf-bg)' } : { fill: 'none', stroke: 'var(--lf-data)' }} />
                  <text x={x + 6} y={labelY[n]} fontSize={10} style={{ fill: 'var(--lf-txt)' }}>{r.header.actors[i]!.id}</text>
                </g>
              )
            })}
            <g transform={`translate(10 ${PX - 12})`}>
              <line x1={0} x2={(bar / frame.size) * PX} y1={0} y2={0} strokeWidth={2} style={{ stroke: 'var(--lf-mut)' }} />
              <text x={0} y={-4} fontSize={10} style={{ fill: 'var(--lf-mut)' }}>{`${bar} m`}</text>
            </g>
            <text x={PX - 10} y={16} fontSize={10} textAnchor="end" style={{ fill: 'var(--lf-mut)' }}>N</text>
          </svg>
          <p className="font-mono text-hud-sub tabular-nums text-muted-foreground">{t('lab.event.clock', { t: (tEvNs / 1e9).toFixed(1) })}</p>
          <table className="w-full text-hud-sub">
            <thead className="text-muted-foreground">
              <tr><th className="text-left font-normal">{t('lab.event.actor')}</th><th className="text-left font-normal">{t('lab.event.state')}</th>
                <th className="text-right font-normal">{t('lab.event.speed')}</th></tr>
            </thead>
            <tbody className="tabular-nums">
              {Array.from(st.ids, (i, j) => (
                <tr key={i} data-actor-row={r.header.actors[i]!.id}>
                  <td className="font-mono">{r.header.actors[i]!.id}</td>
                  <td>{t(`lab.event.states.${STATES[st.state[j]!] ?? 'absent'}`)}</td>
                  <td className="text-right font-mono">{Math.hypot(st.vel[3 * j]!, st.vel[3 * j + 1]!).toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </aside>
  )
}
