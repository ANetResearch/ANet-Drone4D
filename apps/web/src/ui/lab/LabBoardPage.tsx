// /lab/board, internal prototype leaderboard (BN-83, BN-84, BN-97; AWR-17 §18.4; M27 §6.7.9). Per track × lane the four
// metric point estimates with 95 % intervals, per-metric ranks with tie groups ("=" marks a statistical tie), the
// non-dominated tier and the reference baseline rows; no composite score. A row opens the entry detail (per test event
// breakdown, run and invalid-run counts). Internal workspace only; the board has no static copy.
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { sanitizeText } from '@/lib/sanitize'
import { benchGet, type Board, type BoardEntry, type BoardEntryDetail, type BoardIndex } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { boardRows, fmtCi, fmtNum, fmtRank, fmtTier, fmtValue, METRICS } from './board'
import { useBenchSession } from './useBenchSession'

export function LabBoardPage() {
  const t = useT()
  const session = useBenchSession()
  const [version, setVersion] = React.useState<string | null>(null)
  const [track, setTrack] = React.useState<string | null>(null)
  const [lane, setLane] = React.useState<string | null>(null)
  const [open, setOpen] = React.useState<BoardEntry | null>(null)
  const idx = useQuery({ queryKey: ['lab', 'board'], queryFn: () => benchGet<BoardIndex>('/board'), enabled: session !== null })
  const v = version ?? idx.data?.current ?? null
  const q = useQuery({
    queryKey: ['lab', 'leaderboard', v], enabled: session !== null && v !== null,
    queryFn: () => benchGet<Board>(`/leaderboard?board_version=${encodeURIComponent(v ?? '')}`),
  })
  const tr = track ?? q.data?.tracks[0]?.track ?? null
  const ln = lane ?? q.data?.lanes[0]?.lane ?? null
  const rows = boardRows(q.data, tr, ln)
  const cols: LfColumn<BoardEntry>[] = [
    { key: 'tier', label: t('lab.board.col.tier'), align: 'right', width: '4rem', format: fmtTier },
    { key: 'entry', label: t('lab.board.col.entry'), format: (e) => (
      <span className="flex items-center gap-1.5">
        <span>{sanitizeText(e.display_name, 64)}</span>
        {e.baseline ? <Badge variant="outline">{t('lab.board.reference')}</Badge> : null}
        {e.status === 'invalid' ? <Badge variant="secondary">{t('lab.board.invalid')}</Badge> : null}
      </span>
    ) },
    { key: 'team', label: t('lab.board.col.team'), format: (e) => sanitizeText(e.team, 48) },
    ...METRICS.map((k): LfColumn<BoardEntry> => ({
      key: k, label: t(`lab.board.metric.${k}`), align: 'right', format: (e) => (
        <span className="inline-flex flex-col items-end leading-tight">
          <span className="font-mono">{fmtValue(e.metrics[k])}</span>
          <span className="font-mono text-hud-sub text-muted-foreground">{fmtCi(e.metrics[k])}</span>
        </span>
      ),
    })),
    ...METRICS.map((k): LfColumn<BoardEntry> => ({
      key: `rank-${k}`, label: t('lab.board.col.rankOf', { m: t(`lab.board.metric.${k}`) }), align: 'right', width: '4.5rem',
      format: (e) => <span className="font-mono">{fmtRank(rows, e, k)}</span>,
    })),
  ]
  const noBoard = idx.data !== undefined && idx.data.versions.length === 0
  return (
    <section data-view="lab-board" data-figure="lab-board" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Icon icon="perf.bench" />
        <h1 className="text-ed-title font-bold">{t('lab.board.title')}</h1>
        <span className="text-hud-sub text-muted-foreground">{session ? t('lab.session', { name: session.principal, role: t(`lab.role.${session.role}`) }) : ''}</span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {idx.data && idx.data.versions.length > 0 ? (
            <NativeSelect size="sm" aria-label={t('lab.board.version')} value={v ?? ''} onChange={(e) => { setVersion(e.target.value); setTrack(null); setLane(null) }}>
              {idx.data.versions.map((x) => (
                <NativeSelectOption key={x.board_version} value={x.board_version}>
                  {x.current ? t('lab.board.versionCurrent', { v: x.board_version }) : x.board_version}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          ) : null}
          {q.data && q.data.tracks.length > 1 ? (
            <NativeSelect size="sm" aria-label={t('lab.board.track')} value={tr ?? ''} onChange={(e) => setTrack(e.target.value)}>
              {q.data.tracks.map((x) => <NativeSelectOption key={x.track} value={x.track}>{x.track}</NativeSelectOption>)}
            </NativeSelect>
          ) : null}
          {q.data && q.data.lanes.length > 1 ? (
            <NativeSelect size="sm" aria-label={t('lab.board.lane')} value={ln ?? ''} onChange={(e) => setLane(e.target.value)}>
              {q.data.lanes.map((x) => <NativeSelectOption key={x.lane} value={x.lane}>{x.lane}</NativeSelectOption>)}
            </NativeSelect>
          ) : null}
          <Button size="sm" variant="ghost" onClick={() => navigate('/lab/submissions')}>
            <Icon icon="data.package" data-icon="inline-start" />
            {t('lab.sub.title')}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => navigate('/lab/runs')}>
            <Icon icon="data.folder" data-icon="inline-start" />
            {t('lab.runs.title')}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => { void idx.refetch(); void q.refetch() }}>
            <Icon icon="refresh" data-icon="inline-start" />
            {t('lab.refresh')}
          </Button>
        </div>
      </div>
      {q.data ? (
        <dl data-figure="lab-board-meta" className="mb-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-hud-sub text-muted-foreground sm:grid-cols-[auto_1fr_auto_1fr]">
          <dt>{t('lab.board.meta.track')}</dt>
          <dd className="font-mono">{q.data.tracks.filter((x) => x.track === tr).map((x) => `${x.track} · ${x.fleet_ref} · ${x.obs_level} · ${x.physics}`).join('') || '—'}</dd>
          <dt>{t('lab.board.meta.lane')}</dt>
          <dd className="font-mono">{q.data.lanes.filter((x) => x.lane === ln).map((x) => `${x.lane} · ${x.test_events.join(', ')}`).join('') || '—'}</dd>
          <dt>{t('lab.board.meta.seeds')}</dt>
          <dd className="truncate font-mono">{q.data.seed_set_commit.slice(0, 16)}</dd>
          <dt>{t('lab.board.meta.platform')}</dt>
          <dd className="truncate font-mono">{q.data.platform_digest.slice(0, 23)}</dd>
        </dl>
      ) : null}
      {idx.isPending || (v !== null && q.isPending) ? (
        <div className="flex flex-col gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
      ) : idx.isError || q.isError ? (
        <PanelEmpty title={t('lab.unreachable')} description={t('lab.unreachableHint')}
          action={<Button size="sm" variant="outline" onClick={() => { void idx.refetch(); void q.refetch() }}>{t('common.retry')}</Button>} />
      ) : noBoard || !rows.length ? (
        <PanelEmpty title={t('lab.board.empty')} description={t('lab.board.emptyHint')} />
      ) : (
        <>
          <LfTable columns={cols} rows={rows} rowKey={(e) => `${e.track}/${e.lane}/${e.entry_id}`} ariaLabel={t('lab.board.title')} figureId="lab-board-table"
            rowHeight={40} selected={open ? `${open.track}/${open.lane}/${open.entry_id}` : null} onRowClick={(e) => setOpen(open?.entry_id === e.entry_id ? null : e)}
            height={Math.max(240, typeof window !== 'undefined' ? window.innerHeight - 320 : 480)} />
          <p className="mt-2 text-hud-sub text-muted-foreground">{t('lab.board.note')}</p>
          {open && v ? <EntryDetail version={v} entry={open} lane={ln} /> : null}
        </>
      )}
    </section>
  )
}

function EntryDetail({ version, entry, lane }: { version: string; entry: BoardEntry; lane: string | null }) {
  const t = useT()
  const q = useQuery({
    queryKey: ['lab', 'board-entry', version, entry.track, entry.entry_id],
    queryFn: () => benchGet<BoardEntryDetail>(`/board/${encodeURIComponent(entry.track)}/entries/${entry.entry_id.split('/').map(encodeURIComponent).join('/')}?board_version=${encodeURIComponent(version)}`),
  })
  const events = q.data?.lanes.find((l) => l.lane === lane)?.events ?? []
  type Ev = (typeof events)[number]
  const cols: LfColumn<Ev>[] = [
    { key: 'event', label: t('lab.board.detail.event'), format: (r) => <span className="font-mono">{sanitizeText(r.event, 64)}</span> },
    { key: 'runs', label: t('lab.board.detail.runs'), align: 'right', format: (r) => String(r.n_runs) },
    { key: 'invalid', label: t('lab.board.detail.invalid'), align: 'right', format: (r) => String(r.invalid_runs) },
    ...METRICS.map((k): LfColumn<Ev> => ({ key: k, label: t(`lab.board.metric.${k}`), align: 'right', format: (r) => <span className="font-mono">{fmtNum(r.metrics[k])}</span> })),
    { key: 'reasons', label: t('lab.board.detail.reasons'), format: (r) => (r.invalid_reasons.length ? r.invalid_reasons.join(', ') : '—') },
  ]
  return (
    <div data-figure="lab-board-entry" className="mt-4">
      <h2 className="mb-2 text-hud-title font-semibold">{t('lab.board.detail.title', { name: sanitizeText(entry.display_name, 64) })}</h2>
      {q.isPending ? <Skeleton className="h-7 rounded-md" /> : q.isError ? (
        <PanelEmpty title={t('lab.board.detail.unavailable')} />
      ) : (
        <LfTable columns={cols} rows={events} rowKey={(r) => r.event} ariaLabel={t('lab.board.detail.title', { name: entry.display_name })} figureId="lab-board-entry-table" />
      )}
    </div>
  )
}
