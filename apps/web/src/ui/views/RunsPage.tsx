// Recordings overlay page, /runs (AWR-14 §5.4 "Runs 覆盖页", §7.2 empty state, §7.6 E-13; M12 §8.8; D1-ext): one
// table.log row per recorded run from GET /api/runs (R33): run id, world, scenario, start wall clock, duration (sum of
// the segments), segment count, size, keep, compatibility with the current world and data layout, the maximum replay
// rate and the state. Incompatible and CORRUPT rows disable "replay" with the reason in the Tooltip (122
// RECORDING_INCOMPATIBLE); the table's one red is the newest CORRUPT row (hot cell). "Replay" asks for confirmation
// (the whole server switches to replay, every client sees it) and then pauses the live clock and opens the segment.
// Overlay pages cap the canvas at 5 fps while visible.
import { useQuery } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { apiGet } from '@/net/api'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { PanelEmpty } from '@/ui/brand'
import { confirmThen } from '@/ui/actions/ConfirmHost'
import { useConnView } from '@/ui/shell/connView'
import { rateLabel } from '@/ui/layout/timelineGuards'
import { enterReplay, replayDeniedKey } from './replayFlow'
import { useFrameCapOverlay } from './useFrameCapOverlay'

export interface RunSegment { seg: number; state: string | null; closed: boolean; t0_ns: number | null; t1_ns: number | null; bytes: number; speed_max: number; epochs: { epoch: number; valid: boolean }[] }
export interface RunItem {
  run_id: string; world_id: string | null; created_unix_ns: string; keep: boolean; bytes: number; segments: RunSegment[]
  scenario: string | null; compatible: boolean; current: boolean
}

/** duration of a run in seconds (sum of its segments) */
export function runDurationS(r: Pick<RunItem, 'segments'>): number {
  let s = 0
  for (const g of r.segments) if (g.t0_ns !== null && g.t1_ns !== null) s += Math.max(0, (Number(g.t1_ns) - Number(g.t0_ns)) / 1e9)
  return s
}

/** state of a run: CORRUPT if any segment is, OPEN while recording, else CLOSED */
export function runState(r: Pick<RunItem, 'segments'>): 'CORRUPT' | 'OPEN' | 'CLOSED' {
  if (r.segments.some((g) => g.state === 'CORRUPT')) return 'CORRUPT'
  if (r.segments.some((g) => g.state === 'OPEN')) return 'OPEN'
  return 'CLOSED'
}

function wallOf(ns: string): string {
  const ms = Number(ns) / 1e6
  if (!(ms > 0)) return '—'
  const d = new Date(ms)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${fmt.wallTime(ms)}`
}

function ReplayButton({ r }: { r: RunItem }) {
  const t = useT()
  useConnView((s) => s.version)
  const st = runState(r)
  const seat = replayDeniedKey()
  const last = r.segments.at(-1)
  const reason = !r.compatible ? t('runs.incompatible') : st === 'CORRUPT' ? t('runs.corrupt') : !last ? t('runs.noSegments')
    : r.current && st === 'OPEN' ? t('runs.recordingNow') : seat ? t(seat) : null
  const btn = (
    <Button size="xs" variant="outline" disabled={reason !== null} focusableWhenDisabled data-run-replay={r.run_id} className="aria-disabled:opacity-50"
      onClick={() => confirmThen({
        id: 'replay:open', title: t('replay.confirm.title'), body: t('replay.confirm.body'), action: t('replay.confirm.action'),
        run: () => {
          navigate(r.world_id ? `/world/${r.world_id}` : '/')
          void enterReplay(r.run_id, last?.seg ?? 0)
        },
      })}>
      <Icon icon="tl.play" data-icon="inline-start" />
      {t('runs.replay')}
    </Button>
  )
  if (!reason) return btn
  return (
    <Tooltip>
      <TooltipTrigger render={<span className="inline-flex" />}>{btn}</TooltipTrigger>
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  )
}

export function RunsPage() {
  const t = useT()
  useFrameCapOverlay('runs')
  const q = useQuery({ queryKey: ['runs'], queryFn: () => apiGet<{ items: RunItem[] }>('/api/runs?limit=200'), refetchInterval: 10_000 })
  const rows = q.data?.items ?? []
  const corrupt = rows.find((r) => runState(r) === 'CORRUPT')
  const cols: LfColumn<RunItem>[] = [
    { key: 'run', label: t('runs.col.run'), format: (r) => (
      <span className="inline-flex items-center gap-1.5 font-mono">
        {sanitizeText(r.run_id, 40)}
        {r.current ? <Badge variant="secondary">{t('runs.current')}</Badge> : null}
      </span>
    ) },
    { key: 'world', label: t('runs.col.world'), format: (r) => <span className="font-mono">{r.world_id ?? '—'}</span> },
    { key: 'scenario', label: t('runs.col.scenario'), format: (r) => (r.scenario ? <span className="font-mono">{sanitizeText(r.scenario, 48)}</span> : '—') },
    { key: 'start', label: t('runs.col.start'), format: (r) => <span className="font-mono tabular-nums">{wallOf(r.created_unix_ns)}</span> },
    { key: 'dur', label: t('runs.col.duration'), align: 'right', format: (r) => fmt.dur(runDurationS(r)) },
    { key: 'segs', label: t('runs.col.segments'), align: 'right', format: (r) => fmt.count(r.segments.length) },
    { key: 'bytes', label: t('runs.col.size'), align: 'right', format: (r) => fmt.bytes(r.bytes) },
    { key: 'speed', label: t('runs.col.speedMax'), align: 'right', format: (r) => rateLabel(Math.min(...r.segments.map((g) => g.speed_max ?? 20), 20)) },
    { key: 'compat', label: t('runs.col.compatible'), format: (r) => (r.compatible ? t('runs.compatible') : t('runs.incompatibleShort')) },
    { key: 'state', label: t('runs.col.state'), format: (r) => (
      <span className="inline-flex items-center gap-1">
        {runState(r) === 'CORRUPT' ? <Icon icon="alert.critical" /> : null}
        {t(`runs.state.${runState(r)}`)}
        {r.keep ? <Badge variant="outline">{t('runs.keep')}</Badge> : null}
      </span>
    ) },
    { key: 'act', label: '', align: 'right', format: (r) => <ReplayButton r={r} /> },
  ]
  return (
    <section data-view="runs" data-figure="runs" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon icon="data.folder" />
        <h1 className="text-ed-title font-bold">{t('runs.title')}</h1>
        <span className="text-hud-sub text-muted-foreground">{t('runs.subtitle')}</span>
        <Button size="sm" variant="ghost" className="ml-auto" onClick={() => (history.length > 1 ? history.back() : navigate('/'))}>
          <Icon icon="close" data-icon="inline-start" />
          {t('common.close')}
        </Button>
      </div>
      {q.isPending ? (
        <div className="flex flex-col gap-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
      ) : q.isError ? (
        <PanelEmpty title={t('runs.unavailable')} description={t('runs.unavailableHint')}
          action={<Button size="sm" variant="outline" onClick={() => void q.refetch()}>{t('common.retry')}</Button>} />
      ) : !rows.length ? (
        <PanelEmpty title={t('runs.empty')} description={t('runs.emptyHint')} />
      ) : (
        <LfTable columns={cols} rows={rows} rowKey={(r) => r.run_id} height={Math.max(240, typeof window !== 'undefined' ? window.innerHeight - 140 : 480)}
          hot={corrupt ? { row: corrupt.run_id, col: 'state' } : null} ariaLabel={t('runs.title')} figureId="runs-table" />
      )}
    </section>
  )
}
