// 运行状态侧栏（/lab/runs?run=<rid>；BN-43；M27 §6.3；AWR-14 §21.6；D3-AC-51）：状态徽标与重启副文、原因码（短文案与处理建议）、
// 实验与事件、项目与提交者、优先级类与划分、种子承诺、入队、开始与结束时刻、取消请求（请求者、时刻与 10 s 时限），尝试记录
// attempts[]（每次尝试各自的状态、重启次数与原因）与状态迁移审计 transitions[]。目录运行只显示清单状态与说明。
import { useQuery } from '@tanstack/react-query'
import { tOr, useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { sanitizeText } from '@/lib/sanitize'
import { BenchError, benchGet } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import { Button } from '@/ui/components/ui/button'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { ReasonBlock } from '@/ui/lab/worlds/ReasonBlock'
import { fmtUnixNs, shortHash } from '@/ui/lab/worlds/worlds'
import { RunReason, RunStateBadge } from './RunCells'
import { isActive, pollInterval, type Confirm, type RunAttempt, type RunDetail, type RunTransition } from './runs'

export const runQueryKey = (rid: string) => ['lab', 'run', rid] as const

export function RunDetailBody({ rid, enabled = true, busy = false, onAsk }: { rid: string; enabled?: boolean; busy?: boolean; onAsk?: (c: Confirm) => void }) {
  const t = useT()
  const q = useQuery({ queryKey: runQueryKey(rid), queryFn: () => benchGet<RunDetail>(`/runs/${encodeURIComponent(rid)}`), enabled,
    refetchInterval: (query) => pollInterval(query.state.data ? [query.state.data] : undefined),
    retry: (n, e) => !(e instanceof BenchError && e.status < 500) && n < 2 })
  const d = q.data
  if (q.isPending) return <div className="flex flex-col gap-2 p-4">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-6 rounded-md" />)}</div>
  if (q.isError || !d) {
    const why = q.error instanceof BenchError ? `${q.error.code ?? q.error.status} ${q.error.reasonName ?? ''}` : ''
    return <div className="p-4"><PanelEmpty title={t('lab.runs.detail.unavailable')} description={why} /></div>
  }
  const attCols: LfColumn<RunAttempt>[] = [
    { key: 'attempt', label: t('lab.runs.detail.col.attempt'), align: 'right', format: (a) => <span className="font-mono">{t('lab.runs.attempt', { n: a.attempt })}</span> },
    { key: 'state', label: t('lab.runs.detail.col.state'), format: (a) => <RunStateBadge r={{ state: a.state, managed: true, restart_count: 0 }} /> },
    { key: 'restarts', label: t('lab.runs.detail.col.restarts'), align: 'right', format: (a) => <span className="font-mono">{a.restart_count}</span> },
    { key: 'started', label: t('lab.runs.detail.col.started'), format: (a) => <span className="font-mono">{fmtUnixNs(a.started_unix_ns)}</span> },
    { key: 'ended', label: t('lab.runs.detail.col.ended'), format: (a) => <span className="font-mono">{fmtUnixNs(a.ended_unix_ns)}</span> },
    { key: 'reason', label: t('lab.runs.detail.col.reason'), format: (a) => <RunReason r={a.reason} /> },
  ]
  const trCols: LfColumn<RunTransition>[] = [
    { key: 'time', label: t('lab.runs.detail.col.time'), format: (x) => <span className="font-mono">{fmtUnixNs(x.t_unix_ns)}</span> },
    { key: 'attempt', label: t('lab.runs.detail.col.attempt'), align: 'right', format: (x) => <span className="font-mono">{t('lab.runs.attempt', { n: x.attempt })}</span> },
    { key: 'from', label: t('lab.runs.detail.col.from'), format: (x) => (x.from ? tOr(`lab.runs.state.${x.from}`, sanitizeText(x.from, 24)) : '—') },
    { key: 'to', label: t('lab.runs.detail.col.to'), format: (x) => tOr(`lab.runs.state.${x.to}`, sanitizeText(x.to, 24)) },
    { key: 'reason', label: t('lab.runs.detail.col.reason'), format: (x) => <span className="font-mono">{x.reason_code ?? '—'}</span> },
    { key: 'actor', label: t('lab.runs.detail.col.actor'), format: (x) => <span className="font-mono">{sanitizeText(x.actor ?? '—', 48)}</span> },
  ]
  const rows: [string, string][] = d.managed ? [
    [t('lab.runs.detail.experiment'), d.experiment_id ?? '—'],
    [t('lab.runs.detail.event'), d.event_id],
    [t('lab.runs.detail.tenant'), d.tenant ?? '—'],
    [t('lab.runs.detail.submittedBy'), d.submitted_by ?? '—'],
    [t('lab.runs.detail.priority'), d.priority_class ?? '—'],
    [t('lab.runs.detail.split'), d.split ?? '—'],
    [t('lab.runs.detail.seed'), shortHash(d.seed_commit)],
    [t('lab.runs.detail.queuedAt'), fmtUnixNs(d.queued_unix_ns)],
    [t('lab.runs.detail.startedAt'), fmtUnixNs(d.started_unix_ns)],
    [t('lab.runs.detail.endedAt'), fmtUnixNs(d.ended_unix_ns)],
  ] : [[t('lab.runs.detail.event'), d.event_id]]
  return (
    <div className="flex flex-col gap-4 p-4" data-figure="lab-run-detail" data-run-id={d.run_id} data-run-active={isActive(d) ? '' : undefined}>
      <div className="flex flex-wrap items-center gap-2">
        <RunStateBadge r={d} />
        {d.managed && d.attempt ? <span className="font-mono text-hud-sub text-muted-foreground">{t('lab.runs.attempt', { n: d.attempt })}</span> : null}
        <div className="ml-auto flex gap-2">
          {d.has_frames ? (
            <Button size="sm" variant="ghost" onClick={() => navigate(`/lab/runs/${encodeURIComponent(d.run_id)}`)}>
              <Icon icon="sensor.camera" data-icon="inline-start" />{t('lab.runs.op.open')}
            </Button>
          ) : null}
          {d.actions.cancel && onAsk ? (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onAsk({ kind: 'cancel', run: d })} data-run-detail-cancel="">
              <Icon icon="mission.abort" data-icon="inline-start" />{t('lab.runs.op.cancel')}
            </Button>
          ) : null}
          {d.actions.retry && onAsk ? (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onAsk({ kind: 'retry', run: d })} data-run-detail-retry="">
              <Icon icon="refresh" data-icon="inline-start" />{t('lab.runs.op.retry')}
            </Button>
          ) : null}
        </div>
      </div>
      {!d.managed ? <p className="text-hud-sub text-muted-foreground" data-run-unmanaged-note="">{t('lab.runs.detail.unmanaged')}</p> : null}
      {d.reason ? <ReasonBlock r={d.reason} /> : null}
      <dl className="grid grid-cols-[minmax(8rem,auto)_1fr] gap-x-4 gap-y-1 text-hud-sub" data-figure="lab-run-fields">
        {rows.map(([k, v]) => (
          <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd className="font-mono">{sanitizeText(v, 96)}</dd></div>
        ))}
        {d.cancel ? (
          <div className="contents" data-run-cancel-request="">
            <dt className="text-muted-foreground">{t('lab.runs.detail.cancel')}</dt>
            <dd>{t('lab.runs.detail.cancelValue', { by: sanitizeText(d.cancel.by ?? '—', 48), at: fmtUnixNs(d.cancel.requested_unix_ns), deadline: fmtUnixNs(d.cancel.deadline_unix_ns) })}</dd>
          </div>
        ) : null}
      </dl>
      {d.managed ? (
        <>
          <div>
            <h3 className="mb-1 text-hud-title font-semibold">{t('lab.runs.detail.attempts')}</h3>
            <LfTable columns={attCols} rows={d.attempts ?? []} rowKey={(a) => String(a.attempt)} ariaLabel={t('lab.runs.detail.attempts')} figureId="lab-run-attempts" height={200} />
          </div>
          <div>
            <h3 className="mb-1 text-hud-title font-semibold">{t('lab.runs.detail.transitions')}</h3>
            {d.transitions?.length ? (
              <LfTable columns={trCols} rows={d.transitions} rowKey={(_x, i) => String(i)} ariaLabel={t('lab.runs.detail.transitions')} figureId="lab-run-transitions" height={280} />
            ) : <p className="text-hud-sub text-muted-foreground">{t('lab.runs.detail.noTransitions')}</p>}
          </div>
        </>
      ) : null}
    </div>
  )
}
