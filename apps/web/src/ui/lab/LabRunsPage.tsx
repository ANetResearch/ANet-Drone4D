// /lab/runs, run list of the internal workspace (AWR-14 §21.2.2, §21.6; BN-42). P0a-1 lists capture and benchmark runs
// read-only; a row with camera frames opens the observation wall. Env benchmark runs (kind = bench) add the policy, the
// physics tier, the four evaluation components (BN-47 summary) and ranked validity.
import { useQuery } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { sanitizeText } from '@/lib/sanitize'
import { benchGet, type BenchRunItem } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import { Button } from '@/ui/components/ui/button'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { fmtNum, METRICS } from './board'
import { useBenchSession } from './useBenchSession'

function kindKey(kind: string): string {
  return kind === 'capture' ? 'capture' : kind === 'bench' ? 'bench' : 'run'
}

export function LabRunsPage() {
  const t = useT()
  const session = useBenchSession()
  const q = useQuery({ queryKey: ['lab', 'runs'], queryFn: () => benchGet<{ items: BenchRunItem[] }>('/runs'), enabled: session !== null, refetchInterval: 15_000 })
  const rows = q.data?.items ?? []
  const cols: LfColumn<BenchRunItem>[] = [
    { key: 'run', label: t('lab.runs.col.run'), format: (r) => <span className="font-mono">{sanitizeText(r.run_id, 64)}</span> },
    { key: 'event', label: t('lab.runs.col.event'), format: (r) => <span className="font-mono">{sanitizeText(r.event_id, 64)}</span> },
    { key: 'kind', label: t('lab.runs.col.kind'), format: (r) => (r.kind ? t(`lab.runs.kind.${kindKey(r.kind)}`) : '—') },
    { key: 'world', label: t('lab.runs.col.world'), format: (r) => <span className="font-mono">{r.world_id ?? '—'}</span> },
    { key: 'policy', label: t('lab.runs.col.policy'), format: (r) => <span className="font-mono">{r.policy ? sanitizeText(r.policy, 48) : '—'}</span> },
    { key: 'physics', label: t('lab.runs.col.physics'), format: (r) => <span className="font-mono">{r.physics ? sanitizeText(r.physics, 24) : '—'}</span> },
    { key: 'state', label: t('lab.runs.col.state'), format: (r) => sanitizeText(r.state, 24) },
    ...METRICS.map((k): LfColumn<BenchRunItem> => ({
      key: k, label: t(`lab.board.metric.${k}`), align: 'right', format: (r) => <span className="font-mono">{fmtNum(r.evaluation?.components[k])}</span>,
    })),
    { key: 'valid', label: t('lab.runs.col.valid'), format: (r) => (r.kind !== 'bench' ? '—' : !r.evaluation ? t('lab.runs.valid.pending')
      : r.evaluation.ranked_valid ? t('lab.runs.valid.yes') : `${t('lab.runs.valid.no')} · ${r.evaluation.invalid_reasons.join(', ')}`) },
    { key: 'frames', label: t('lab.runs.col.frames'), format: (r) => (r.has_frames ? t('lab.runs.frames.yes') : t('lab.runs.frames.no')) },
  ]
  return (
    <section data-view="lab-runs" data-figure="lab-runs" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon icon="data.folder" />
        <h1 className="text-ed-title font-bold">{t('lab.runs.title')}</h1>
        <span className="text-hud-sub text-muted-foreground">{session ? t('lab.session', { name: session.principal, role: t(`lab.role.${session.role}`) }) : ''}</span>
        <Button size="sm" variant="ghost" className="ml-auto" onClick={() => navigate('/lab/board')}>
          <Icon icon="perf.bench" data-icon="inline-start" />
          {t('lab.board.title')}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => void q.refetch()}>
          <Icon icon="refresh" data-icon="inline-start" />
          {t('lab.refresh')}
        </Button>
      </div>
      {q.isPending ? (
        <div className="flex flex-col gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
      ) : q.isError ? (
        <PanelEmpty title={t('lab.unreachable')} description={t('lab.unreachableHint')}
          action={<Button size="sm" variant="outline" onClick={() => void q.refetch()}>{t('common.retry')}</Button>} />
      ) : !rows.length ? (
        <PanelEmpty title={t('lab.runs.empty')} description={t('lab.runs.emptyHint')} />
      ) : (
        <LfTable columns={cols} rows={rows} rowKey={(r) => r.run_id} ariaLabel={t('lab.runs.title')} figureId="lab-runs-table"
          onRowClick={(r) => r.has_frames && navigate(`/lab/runs/${encodeURIComponent(r.run_id)}`)}
          height={Math.max(240, typeof window !== 'undefined' ? window.innerHeight - 140 : 480)} />
      )}
    </section>
  )
}
