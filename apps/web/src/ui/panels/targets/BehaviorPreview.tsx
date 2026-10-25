import * as React from 'react'
import { useT } from '@/app/i18n'
import { lifeApi, type LifeRehearsal, type LifeSample } from '@/ui/sandbox/lifeApi'
import { Button } from '@/ui/components/ui/button'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Slider } from '@/ui/components/ui/slider'
import { rehearsalFrame } from './rehearsalFrame'

/** Isolated authoring replay. This component has no simulation control or configuration write access. */
export function BehaviorPreview({ sha, actorId, onFrame }: { sha: string; actorId: string; onFrame: (frame: LifeSample | null) => void }) {
  const t = useT()
  const [data, setData] = React.useState<LifeRehearsal | null>(null)
  const [status, setStatus] = React.useState<'idle' | 'loading' | 'error' | 'ready'>('idle')
  const [time, setTime] = React.useState(0)
  const [playing, setPlaying] = React.useState(false)
  const [speed, setSpeed] = React.useState(10)
  const active = React.useRef(true)
  React.useEffect(() => { active.current = true; return () => { active.current = false; onFrame(null) } }, [onFrame])
  const load = async () => {
    setStatus('loading')
    try {
      const result = await lifeApi.rehearsal(sha, actorId)
      if (active.current) { setData(result); setStatus('ready'); setTime(0) }
    } catch { if (active.current) setStatus('error') }
  }
  React.useEffect(() => {
    if (!playing || !data) return
    let previous = performance.now()
    const timer = window.setInterval(() => {
      const now = performance.now(), dt = (now - previous) / 1000; previous = now
      setTime((value) => Math.min(data.duration_s, value + dt * speed))
    }, 50)
    return () => window.clearInterval(timer)
  }, [playing, data, speed])
  const frame = React.useMemo(() => data ? rehearsalFrame(data.samples, time) : null, [data, time])
  React.useEffect(() => { onFrame(frame) }, [frame, onFrame])
  const map = React.useMemo(() => {
    if (!data) return null
    const points = [...data.roads.flat(), ...data.samples.map((s) => s.pos)]
    const xs = points.map((p) => p[0]), ys = points.map((p) => p[1])
    const left = Math.min(...xs), bottom = Math.min(...ys)
    const width = Math.max(...xs) - left, height = Math.max(...ys) - bottom
    const scale = 260 / Math.max(width, height, 1)
    const project = (p: number[]) => [20 + (p[0] - left) * scale, 280 - (p[1] - bottom) * scale]
    return { project, line: (points: number[][]) => points.map((p) => project(p).join(',')).join(' ') }
  }, [data])
  const mapBackground = React.useMemo(() => data && map ? <>
    {data.roads.map((road, i) => <polyline key={i} points={map.line(road)} fill="none" stroke="currentColor" strokeOpacity=".18" strokeWidth="2" />)}
    <polyline className="text-brand-text" points={map.line(data.samples.map((s) => s.pos))} fill="none" stroke="currentColor" strokeWidth="2" />
    {data.facilities.map((f) => { const [x, y] = map.project(f.pos); return <rect key={f.id} x={x - 2} y={y - 2} width={4} height={4} fill="currentColor" opacity=".5"><title>{f.label}</title></rect> })}
  </> : null, [data, map])
  return <section className="mt-3 flex flex-col gap-2 border-t pt-3 text-hud-sub" data-behavior-preview="">
    <strong>{t('targets.rehearsal.title')}</strong>
    <p className="text-muted-foreground">{t('targets.rehearsal.scope')}</p>
    {status !== 'ready' ? <Button size="sm" variant="outline" disabled={status === 'loading'} onClick={() => void load()}>{t(status === 'loading' ? 'targets.rehearsal.loading' : 'targets.rehearsal.load')}</Button> : null}
    {status === 'error' ? <p role="alert">{t('targets.rehearsal.error')}</p> : null}
    {data && frame && map ? <>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => { if (time >= data.duration_s) { setTime(0); setPlaying(true) } else setPlaying(!playing) }}>{t(playing && time < data.duration_s ? 'targets.rehearsal.pause' : 'targets.rehearsal.play')}</Button>
        <label>{t('targets.rehearsal.speed')} <NativeSelect aria-label={t('targets.rehearsal.speed')} value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>{[1, 10, 30].map((v) => <NativeSelectOption key={v} value={v}>{v}×</NativeSelectOption>)}</NativeSelect></label>
        <span>{Math.floor(time / 60)}:{String(Math.floor(time % 60)).padStart(2, '0')} / 25:00</span>
      </div>
      <Slider min={0} max={data.duration_s} step={1} value={[time]} aria-label={t('targets.rehearsal.time')} onValueChange={(v) => { setPlaying(false); setTime(Array.isArray(v) ? v[0] : v) }} />
      <p data-rehearsal-state={frame.phase}>{t(`activity.phase.${frame.phase}`)} · {frame.destination} · {frame.speed.toFixed(1)} m/s · {Math.round(frame.distance)} m</p>
      {frame.reason ? <p>{t('targets.activityCard.reason')}{frame.reason}</p> : null}
      {frame.hidden ? <p>{t('targets.activityCard.inside_the_registered_facility_identity_retained')}</p> : null}
      <svg viewBox="0 0 300 300" className="max-h-64 w-full rounded border bg-muted/30" role="img" aria-label={t('targets.rehearsal.map')}>
        {mapBackground}
        <g className="text-brand-text" transform={`translate(${map.project(frame.pos).join(' ')}) rotate(${-frame.yaw * 180 / Math.PI})`}><circle r="6" fill="var(--background)" stroke="currentColor" strokeWidth="2" strokeDasharray={frame.hidden ? '2 2' : undefined} /><path d="M 5 -3 L 11 0 L 5 3" fill="none" stroke="currentColor" strokeWidth="2" /></g>
      </svg>
      <p className="text-muted-foreground">{t('targets.rehearsal.legend', { seed: data.seed })}</p>
      <details><summary className="cursor-pointer">{t('targets.rehearsal.itinerary')}</summary><ol className="list-decimal pl-5">{data.itinerary.map((visit, i) => <li key={i}>{data.facilities.find((f) => f.id === visit.facility)?.label ?? visit.facility} · {visit.purpose} · {visit.dwell_s} s</li>)}</ol></details>
    </> : null}
  </section>
}
