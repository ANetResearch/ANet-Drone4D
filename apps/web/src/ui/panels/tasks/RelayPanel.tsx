// Dock "relay" tab: the 7x24 relay monitoring status (R-D2-05, R-D2-06, R-D2-19; M15-FR-214, FR-220; UX-FR-131; AWR-14
// §20.9.4 "接力子页", §20.13.4; AWR-04 §9.5): one relay task at a time (Select; the selected task of the tasking store when
// it is a relay), its target and level, the six KPI (coverage of the monitored time, gaps with the longest, discovered
// count: red text when > 0, the table's hero; handovers with the mean overlap, the smallest keep-out margin, the fewest
// ready packs), the stations with the vehicle on station and its time on station (plan geometry + `on_station`), the next
// relief with a countdown (eta of the 1 Hz statistics counted down locally between messages), the gaps by cause with
// their total seconds, dispatch delays and the sortie and energy totals; the coverage history of the session (P1, static
// lieflat line of the last 120 one-second samples). Read-only for everyone (no write requests, D2-AC-37); the panel
// re-renders at the store rate (1 Hz data), the countdown and the time on station are bound texts (<= 4 Hz, no render).
import * as React from 'react'
import { tOr, useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { camera } from '@/viewport/facade'
import { selection } from '@/stores/selection'
import { taskingActions, taskingStore, useTasking, type TaskRow } from '@/stores/tasking'
import { timelineStore } from '@/stores/timeline'
import { useTargets } from '@/stores/targets'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Icon } from '@/ui/icons/Icon'
import { LfLine } from '@/ui/lf/LfLine'
import { LfStat } from '@/ui/lf/LfStat'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { PanelEmpty } from '@/ui/brand'
import { BoundText } from '@/ui/motion/BoundText'
import { GAP_CAUSES, mmss, reliefLeftS, stationRows, type StationRow } from './taskModel'

// the next relief's eta is counted down from the wall time its value arrived (C-class text, bindText <= 4 Hz)
const etaSeen = new Map<string, { key: string; atMs: number }>()
function reliefReader(tid: string): () => number {
  return () => {
    const n = taskingStore.getState().relay[tid]?.next
    const key = `${n?.uav ?? ''}|${n?.eta_s ?? ''}`
    let e = etaSeen.get(tid)
    if (!e || e.key !== key) {
      e = { key, atMs: Date.now() }
      etaSeen.set(tid, e)
    }
    return reliefLeftS(n?.eta_s, e.atMs, Date.now()) ?? Number.NaN
  }
}
const fmtLeft = (v: number): string => (Number.isFinite(v) ? mmss(v) : '-')
/** time on station: sim now minus the sim time the vehicle took the station */
const onStationReader = (sinceSimS: number) => (): number => Math.max(0, timelineStore.getState().tDisplayS - sinceSimS)

function useRelayChoice(relays: readonly TaskRow[]): [string | null, (tid: string) => void] {
  const selected = useTasking((s) => s.selected)
  const [own, setOwn] = React.useState<string | null>(null)
  const pick = relays.find((r) => r.tid === selected)?.tid ?? (own && relays.some((r) => r.tid === own) ? own : null) ?? relays.find((r) => r.state === 'RUNNING')?.tid ?? relays[0]?.tid ?? null
  return [pick, (tid: string) => {
    setOwn(tid)
    if (taskingStore.getState().selected !== tid) taskingActions.select(tid)
  }]
}

function SinceText({ sinceSimS }: { sinceSimS: number }) {
  const read = React.useMemo(() => onStationReader(sinceSimS), [sinceSimS])
  return <BoundText read={read} format={mmss} />
}

export function RelayPanel() {
  const t = useT()
  const tasks = useTasking((s) => s.tasks)
  const relays = React.useMemo(() => tasks.filter((r) => r.type === 'relay_watch'), [tasks])
  const [tid, choose] = useRelayChoice(relays)
  const stats = useTasking((s) => (tid ? s.relay[tid] ?? null : null))
  const history = useTasking((s) => (tid ? s.relayHistory[tid] ?? null : null))
  const geometry = useTasking((s) => (tid ? s.plans[tid]?.geometry ?? null : null))
  const targetLabel = useTargets((s) => {
    const id = stats?.target
    return id ? (s.items.find((x) => x.id === id)?.label ?? id) : null
  })
  const readLeft = React.useMemo(() => reliefReader(tid ?? ''), [tid])
  if (relays.length === 0) {
    return <PanelEmpty brand={false} title={t('relay.empty')} description={t('relay.emptyHint')} />
  }
  const row = relays.find((r) => r.tid === tid) ?? null
  const stations = stationRows(stats, geometry?.stations)
  const cols: LfColumn<StationRow>[] = [
    { key: 'station', label: t('relay.col.station'), format: (r) => <span className="font-mono">{sanitizeText(r.station, 24)}</span> },
    { key: 'role', label: t('relay.col.role'), format: (r) => t(`relay.role.${r.role === 'handover' ? 'handover' : 'primary'}`) },
    { key: 'uav', label: t('relay.col.uav'), format: (r) => (r.uav ? (
      <Button size="xs" variant="ghost" className="h-5 px-1 font-mono" onClick={() => {
        if (!r.uav) return
        selection.select([r.uav])
        camera.focus([r.uav])
      }}>{sanitizeText(r.uav, 24)}</Button>
    ) : <span className="text-muted-foreground">{t('relay.vacant')}</span>) },
    { key: 'since', label: t('relay.col.since'), align: 'right', format: (r) => (r.sinceSimS !== null && r.uav
      ? <SinceText key={`${r.station}:${r.uav}:${r.sinceSimS}`} sinceSimS={r.sinceSimS} /> : '-') },
  ]
  const gaps = stats?.gaps
  const delays = stats?.dispatch_delays ?? {}
  return (
    <div className="flex min-h-0 flex-col gap-2" data-relay-panel={tid ?? ''}>
      <div className="flex flex-wrap items-center gap-2">
        <Select items={relays.map((r) => ({ value: r.tid, label: r.label || r.tid }))} value={tid ?? ''} onValueChange={(v) => typeof v === 'string' && choose(v)}>
          <SelectTrigger size="sm" className="w-56" data-relay-select=""><SelectValue /></SelectTrigger>
          <SelectContent>
            {relays.map((r) => <SelectItem key={r.tid} value={r.tid}>{`${r.label || t('task.type.relay_watch')} · ${r.tid}`}</SelectItem>)}
          </SelectContent>
        </Select>
        {row ? <Badge variant="outline" data-relay-state={row.state}>{t(`task.state.${row.state}`)}</Badge> : null}
        {stats ? (
          <span className="text-hud-sub text-muted-foreground" data-relay-target={stats.target}>
            {t('relay.target', { target: sanitizeText(targetLabel ?? stats.target, 32), level: (row?.params.level_req as string | undefined) ?? '-', t: mmss(stats.t_monitor_s) })}
          </span>
        ) : null}
      </div>
      {!stats ? <p className="text-hud-sub text-muted-foreground">{t('relay.waiting')}</p> : (
        <>
          <div className="grid grid-cols-3 items-start gap-x-4 gap-y-2 xl:grid-cols-6" data-relay-kpi="">
            <LfStat label={t('relay.kpi.coverage')} unit="%" value={stats.coverage * 100} format={(v) => fmt.num(v, 1)} />
            <div className="flex flex-col gap-0.5">
              <LfStat label={t('relay.kpi.gapsN')} value={gaps?.count ?? 0} format={(v) => fmt.num(v, 0)} cls="D" />
              <span className="text-hud-sub text-muted-foreground" data-relay-maxgap="">{t('relay.kpi.maxGap', { s: fmt.num(gaps?.max_s ?? 0, 0) })}</span>
            </div>
            <LfStat label={t('relay.kpi.discovered')} value={stats.discovered} format={(v) => fmt.num(v, 0)} hero={stats.discovered > 0} cls="D" />
            <div className="flex flex-col gap-0.5">
              <LfStat label={t('relay.kpi.handoversN')} value={stats.handovers.count} format={(v) => fmt.num(v, 0)} cls="D" />
              <span className="text-hud-sub text-muted-foreground">
                {t('relay.kpi.overlap', { s: stats.handovers.mean_overlap_s !== null && stats.handovers.mean_overlap_s !== undefined ? fmt.num(stats.handovers.mean_overlap_s, 0) : '-' })}
              </span>
            </div>
            <LfStat label={t('relay.kpi.margin')} unit="m" value={stats.min_keepout_margin_m ?? Number.NaN} format={(v) => (Number.isFinite(v) ? fmt.num(v, 1) : '-')} />
            <LfStat label={t('relay.kpi.packs')} value={stats.min_ready_packs ?? Number.NaN} format={(v) => (Number.isFinite(v) ? fmt.num(v, 0) : '-')} />
          </div>
          <div className="grid gap-3 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('relay.stations', { n: stations.length })}</span>
              {stations.length ? (
                <LfTable columns={cols} rows={stations} rowKey={(r) => r.station} ariaLabel={t('relay.stations', { n: stations.length })} figureId="relay-stations"
                  height={Math.min(150, 32 + 28 * stations.length)} />
              ) : <span className="text-hud-sub text-muted-foreground">{t('relay.noStation')}</span>}
            </div>
            <div className="flex min-w-0 flex-col gap-1.5 text-hud-sub">
              <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('relay.next')}</span>
              {stats.next?.uav ? (
                <span className="flex items-center gap-2" data-relay-next={stats.next.uav}>
                  <Icon icon="task.relay" />
                  <span className="font-mono">{sanitizeText(stats.next.uav, 24)}</span>
                  <BoundText read={readLeft} format={fmtLeft} className="font-mono text-hud-kpi font-extrabold" data-relay-countdown="" />
                  <span className="text-muted-foreground">{stats.next.reason ? tOr(`relay.reason.${stats.next.reason}`, stats.next.reason) : ''}</span>
                </span>
              ) : <span className="text-muted-foreground">{t('relay.noNext')}</span>}
              <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('relay.gapsByCause')}</span>
              <span className="grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono" data-relay-gaps={gaps?.count ?? 0}>
                {GAP_CAUSES.map((c) => (
                  <span key={c} className="flex justify-between gap-2">
                    <span className="truncate font-sans text-muted-foreground">{t(`relay.cause.${c}`)}</span>
                    <span className="tabular-nums">{`${gaps?.by_cause[c] ?? 0} · ${fmt.num(gaps?.total_s_by_cause?.[c] ?? 0, 0)} s`}</span>
                  </span>
                ))}
              </span>
              <span className="text-muted-foreground" data-relay-energy="">
                {t('relay.energy', { sorties: stats.sorties ?? 0, h: fmt.num(stats.flight_h ?? 0, 1), wh: fmt.num(stats.energy_wh ?? 0, 0), rtl: stats.energy_rtl ?? 0 })}
              </span>
              {Object.values(delays).some((v) => (v ?? 0) > 0) ? (
                <span className="text-muted-foreground" data-relay-delays="">
                  {t('relay.delays', { list: Object.entries(delays).filter(([, v]) => (v ?? 0) > 0).map(([k, v]) => `${tOr(`relay.delay.${k}`, k)} ${v}`).join(t('common.listSep')) })}
                </span>
              ) : null}
            </div>
          </div>
          {history && history.length >= 3 ? (
            <div className="flex flex-col gap-1" data-relay-history={history.length}>
              <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('relay.history')}</span>
              <LfLine mode="static" data={history.slice(-120).map((h) => ({ t: h.tMonitorS, v: h.coverage * 100 }))} hero="last" peaks={0} height={72}
                format={(v) => `${fmt.num(v, 1)}%`} ariaLabel={t('relay.history')} />
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
