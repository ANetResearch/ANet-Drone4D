// /lab/events，事件列表与编译状态（AWR-14 §21.4.1、§21.15.4；BN-20、BN-22；M23 §7.3、§7.3.1；D3-AC-51）。LfTable 列：事件 id、名称、
// 世界、最新版本、状态、编译模式、时长、脚本演员、背景演员、磁带 content_sha256 前 12 位、更新于；筛选：世界、状态、关键字（URL
// `world`、`status`、`q`）。行点击（`?eid=`，版本 `?v=`）在右侧 Sheet 打开该版本的编译详情：原因码（短文案、处理建议、门错误）、
// 三道编译门、路段、磁带摘要、发布记录与产物清单。编译失败与不可行的最新一行为表格唯一的 hot 单元。
// 未在本页提供：所有者筛选与"被引用运行数"列（BN-20 未给出所有者与引用计数）、时间线与编辑器（/lab/events/:eid，P0a 只读回放另行交付）。
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getLocale, tOr, useT } from '@/app/i18n'
import { updateSearch, type RouteProps } from '@/app/router/router'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { BenchError, benchGet } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Input } from '@/ui/components/ui/input'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { useBenchSession } from '@/ui/lab/useBenchSession'
import { LabHeader, useDocTitle } from './LabHeader'
import { ReasonBlock } from './ReasonBlock'
import { WORLDS_QUERY } from './WorldsPage'
import {
  EVENT_STATUSES, eventQuery, fmtFixed, fmtUnixNs, gateOrder, issueText, LAB_ID_RE, parseEventFilter, shortHash, worldName,
  type EventDetail, type EventFilter, type EventItem, type WorldItem,
} from './worlds'

/** 路段字段的单位（M23 gen 路段诊断：len_m、avail_s、need_mps） */
const SEG_UNITS: Readonly<Record<string, string>> = { len_m: 'm', avail_s: 's', need_mps: 'm/s' }

/** 路段诊断单元：数值两位小数，字符串原样（截断），其余 JSON */
function cellText(v: unknown): string {
  if (typeof v === 'number') return fmtFixed(v, 2)
  if (v === null || v === undefined) return '—'
  return sanitizeText(typeof v === 'string' ? v : JSON.stringify(v), 48)
}

export const eventsQuery = (f: EventFilter) => ['lab', 'events', f.world, f.status, f.q] as const
export const eventQueryKey = (eid: string, v: number | null) => ['lab', 'event', eid, v] as const

export function EventsPage({ search }: RouteProps) {
  const t = useT()
  const qc = useQueryClient()
  const session = useBenchSession()
  useDocTitle(t('lab.events.docTitle'))
  const f = parseEventFilter(search)
  const eid = search.get('eid')
  const vRaw = Number(search.get('v'))
  const v = Number.isInteger(vRaw) && vRaw > 0 ? vRaw : null
  const [q, setQ] = React.useState(f.q)
  // 关键字输入 300 ms 后写入 URL（replaceState，不产生历史记录）
  React.useEffect(() => {
    if (q === f.q) return
    const h = setTimeout(() => updateSearch({ q: q.trim() || null }), 300)
    return () => clearTimeout(h)
  }, [q, f.q])
  const worlds = useQuery({ queryKey: WORLDS_QUERY, queryFn: () => benchGet<{ items: WorldItem[] }>('/worlds'), enabled: session !== null, staleTime: 30_000 })
  const list = useQuery({ queryKey: eventsQuery(f), queryFn: () => benchGet<{ items: EventItem[]; total: number }>(`/events${eventQuery(f)}`),
    enabled: session !== null, staleTime: 15_000, refetchInterval: 30_000 })
  const rows = list.data?.items ?? []
  const zh = getLocale() === 'zh-CN'
  const bad = rows.find((r) => r.status === 'failed' || r.status === 'infeasible')?.event_id ?? null
  const cols: LfColumn<EventItem>[] = [
    { key: 'id', label: t('lab.events.col.id'), format: (r) => (
      <span className="font-mono">{sanitizeText(r.event_id, 64)}{r.split === 'test' ? <Badge variant="outline" className="ml-1">{t('lab.events.split.test')}</Badge> : null}</span>
    ) },
    { key: 'name', label: t('lab.events.col.name'), format: (r) => sanitizeText(r.name ?? '—', 64) },
    { key: 'world', label: t('lab.events.col.world'), format: (r) => <span className="font-mono">{sanitizeText(r.world.id ?? '—', 64)}</span> },
    { key: 'version', label: t('lab.events.col.version'), align: 'right', format: (r) => <span className="font-mono">{`v${r.version}`}</span> },
    { key: 'status', label: t('lab.events.col.status'), format: (r) => (
      <span data-event-status={r.status} title={r.reason ? `${r.reason.code} ${r.reason.name ?? ''}` : undefined}>
        {tOr(`lab.events.status.${r.status}`, sanitizeText(r.status, 24))}{r.reason ? <span className="font-mono">{` · ${r.reason.code}`}</span> : null}
      </span>
    ) },
    { key: 'strict', label: t('lab.events.col.strict'), format: (r) => (r.strict ? t('lab.events.strict.on') : t('lab.events.strict.off')) },
    { key: 'duration', label: t('lab.events.col.duration'), unit: 's', align: 'right', format: (r) => <span className="font-mono">{fmtFixed(r.duration_s, 1)}</span> },
    { key: 'scripted', label: t('lab.events.col.scripted'), align: 'right', format: (r) => fmt.count(r.n_scripted) },
    { key: 'background', label: t('lab.events.col.background'), align: 'right', format: (r) => fmt.count(r.n_background) },
    { key: 'tape', label: t('lab.events.col.tape'), format: (r) => <span className="font-mono" title={r.tape_content_sha256 ?? undefined}>{shortHash(r.tape_content_sha256)}</span> },
    { key: 'updated', label: t('lab.events.col.updated'), format: (r) => <span className="font-mono">{fmtUnixNs(r.updated_unix_ns)}</span> },
  ]
  const refresh = () => void qc.invalidateQueries({ queryKey: ['lab', 'events'] })
  const filtered = Boolean(f.world || f.status || f.q)
  return (
    <section data-view="lab-events" data-figure="lab-events" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <LabHeader page="events" icon="mission.list" title={t('lab.events.title')} session={session} onRefresh={refresh} />
      <div className="mb-3 flex flex-wrap items-end gap-3" data-figure="lab-events-filter">
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.events.filter.world')}</span>
          <NativeSelect size="sm" value={f.world ?? ''} onChange={(e) => updateSearch({ world: e.target.value || null, eid: null, v: null })} data-events-filter="world">
            <NativeSelectOption value="">{t('lab.events.filter.allWorlds')}</NativeSelectOption>
            {(worlds.data?.items ?? []).map((w) => <NativeSelectOption key={w.world_id} value={w.world_id}>{`${w.world_id} · ${worldName(w, zh)}`}</NativeSelectOption>)}
            {f.world && !(worlds.data?.items ?? []).some((w) => w.world_id === f.world) ? <NativeSelectOption value={f.world}>{f.world}</NativeSelectOption> : null}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.events.filter.status')}</span>
          <NativeSelect size="sm" value={f.status ?? ''} onChange={(e) => updateSearch({ status: e.target.value || null })} data-events-filter="status">
            <NativeSelectOption value="">{t('lab.events.filter.allStatus')}</NativeSelectOption>
            {EVENT_STATUSES.map((s) => <NativeSelectOption key={s} value={s}>{t(`lab.events.status.${s}`)}</NativeSelectOption>)}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.events.filter.q')}</span>
          <Input className="h-7 w-56" value={q} maxLength={80} placeholder={t('lab.events.filter.qPlaceholder')} onChange={(e) => setQ(e.target.value)} data-events-filter="q" />
        </label>
        {filtered ? (
          <Button size="sm" variant="ghost" onClick={() => {
            setQ('')
            updateSearch({ world: null, status: null, q: null })
          }}>{t('lab.events.filter.clear')}</Button>
        ) : null}
        {list.data ? <span className="ml-auto text-hud-sub text-muted-foreground">{t('lab.events.total', { n: list.data.total })}</span> : null}
      </div>
      {list.isPending ? (
        <div className="flex flex-col gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
      ) : list.isError ? (
        <PanelEmpty title={t('lab.unreachable')} description={t('lab.unreachableHint')}
          action={<Button size="sm" variant="outline" onClick={refresh}>{t('common.retry')}</Button>} />
      ) : !rows.length ? (
        filtered ? <PanelEmpty title={t('lab.events.noMatch')} /> : <PanelEmpty title={t('lab.events.empty')} description={t('lab.events.emptyHint')} />
      ) : (
        <LfTable columns={cols} rows={rows} rowKey={(r) => r.event_id} ariaLabel={t('lab.events.title')} figureId="lab-events-table"
          selected={eid} hot={bad ? { row: bad, col: 'status' } : null} onRowClick={(r) => updateSearch({ eid: r.event_id, v: null })}
          height={Math.max(240, typeof window !== 'undefined' ? window.innerHeight - 200 : 480)} />
      )}
      <Sheet open={eid !== null && LAB_ID_RE.test(eid)} onOpenChange={(o) => (o ? null : updateSearch({ eid: null, v: null }))}>
        <SheetContent side="right" className="w-160 max-w-full overflow-auto sm:max-w-160" data-event-sheet={eid ?? ''}>
          <SheetHeader className="pb-0">
            <SheetTitle className="font-mono">{sanitizeText(eid ?? '', 64)}</SheetTitle>
            <SheetDescription>{t('lab.events.detail.description')}</SheetDescription>
          </SheetHeader>
          {eid && LAB_ID_RE.test(eid) ? <EventDetailBody eid={eid} v={v} enabled={session !== null} /> : null}
        </SheetContent>
      </Sheet>
    </section>
  )
}

/** BN-22 编译详情（Sheet 内容；单独导出供测试） */
export function EventDetailBody({ eid, v, enabled = true }: { eid: string; v: number | null; enabled?: boolean }) {
  const t = useT()
  const zh = getLocale() === 'zh-CN'
  const q = useQuery({ queryKey: eventQueryKey(eid, v), queryFn: () => benchGet<EventDetail>(`/events/${encodeURIComponent(eid)}${v ? `?version=${v}` : ''}`),
    enabled, staleTime: 15_000, retry: (n, e) => !(e instanceof BenchError && e.status < 500) && n < 2 })
  const d = q.data
  if (q.isPending) return <div className="flex flex-col gap-2 p-4">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-6 rounded-md" />)}</div>
  if (q.isError || !d) {
    const why = q.error instanceof BenchError ? `${q.error.code ?? q.error.status} ${q.error.reasonName ?? ''}` : ''
    return <div className="p-4"><PanelEmpty title={t('lab.events.detail.unavailable')} description={why} /></div>
  }
  const segCols: LfColumn<Record<string, unknown>>[] = Object.keys(d.segments[0] ?? {}).slice(0, 6).map((k) => ({
    key: k, label: tOr(`lab.events.seg.${k}`, k), unit: SEG_UNITS[k], align: typeof d.segments[0][k] === 'number' ? 'right' : 'left',
    format: (s: Record<string, unknown>) => <span className="font-mono">{cellText(s[k])}</span>,
  }))
  return (
    <div className="flex flex-col gap-4 p-4" data-figure="lab-event-detail" data-event-id={d.event_id} data-event-version={d.version}>
      <h3 className="font-mono text-hud-title font-semibold" data-event-detail-title="">{t('lab.events.detail.title', { id: sanitizeText(d.event_id, 64), v: d.version })}</h3>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" data-event-status={d.status} className={d.status === 'failed' || d.status === 'infeasible' ? 'border-destructive text-brand-text' : undefined}>
          {tOr(`lab.events.status.${d.status}`, sanitizeText(d.status, 24))}
        </Badge>
        <Badge variant="outline">{d.strict ? t('lab.events.strict.on') : t('lab.events.strict.off')}</Badge>
        {d.split === 'test' ? <Badge variant="outline">{t('lab.events.split.test')}</Badge> : null}
        {d.versions.length > 1 ? (
          <label className="ml-auto flex items-center gap-2 text-hud-sub">
            <span className="text-muted-foreground">{t('lab.events.detail.version')}</span>
            <NativeSelect size="sm" value={String(d.version)} onChange={(e) => updateSearch({ v: Number(e.target.value) === d.latest_version ? null : e.target.value })} data-event-version-select="">
              {d.versions.toReversed().map((x) => (
                <NativeSelectOption key={x.version} value={String(x.version)}>{t('lab.events.detail.versionOption', { v: x.version, status: tOr(`lab.events.status.${x.status}`, x.status) })}</NativeSelectOption>
              ))}
            </NativeSelect>
          </label>
        ) : null}
      </div>
      {d.reason ? <ReasonBlock r={d.reason} title={t('lab.events.detail.reason')} /> : null}
      <div>
        <h3 className="mb-1 text-hud-title font-semibold">{t('lab.events.detail.gates')}</h3>
        {!d.gates.length ? <p className="text-hud-sub text-muted-foreground">{t('lab.events.detail.gatesEmpty')}</p> : (
          <ol className="flex flex-col gap-1.5 text-hud-sub" data-figure="lab-event-gates">
            {gateOrder(d.gates).map((g) => (
              <li key={g.gate} data-gate={g.gate} data-gate-status={g.status} className={g.status === 'fail' ? 'rounded-md border border-destructive p-1.5' : 'p-1.5'}>
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{tOr(`lab.events.gate.${g.gate}`, `${g.gate}`)}</span>
                  <span className={g.status === 'fail' ? 'text-brand-text' : 'text-muted-foreground'}>{tOr(`lab.events.gateStatus.${g.status}`, sanitizeText(g.status, 16))}</span>
                  <span className="ml-auto font-mono text-muted-foreground">{t('lab.events.detail.issues', { e: g.errors.length, w: g.warnings.length })}</span>
                </div>
                {[...g.errors, ...g.warnings].length ? (
                  <ul className="mt-1 flex flex-col gap-0.5 pl-4">
                    {[...g.errors.map((i) => ({ i, err: true })), ...g.warnings.map((i) => ({ i, err: false }))].slice(0, 20).map(({ i, err }, k) => (
                      <li key={k} className={err ? '' : 'text-muted-foreground'}>
                        {sanitizeText(issueText(i, zh), 200)}{i.path ? <span className="font-mono text-muted-foreground">{` ${sanitizeText(i.path, 80)}`}</span> : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </div>
      {d.segments.length ? (
        <div>
          <h3 className="mb-1 text-hud-title font-semibold">{t('lab.events.detail.segments')}<span className="ml-2 font-normal text-muted-foreground">{t('lab.events.detail.segmentsCount', { n: d.segments.length })}</span></h3>
          <LfTable columns={segCols} rows={d.segments} rowKey={(_s, i) => String(i)} ariaLabel={t('lab.events.detail.segments')} figureId="lab-event-segments" height={200} />
        </div>
      ) : null}
      <div>
        <h3 className="mb-1 text-hud-title font-semibold">{t('lab.events.detail.tape')}</h3>
        {!d.tape ? <p className="text-hud-sub text-muted-foreground">{t('lab.events.detail.tapeNone')}</p> : (
          <dl className="grid grid-cols-[minmax(8rem,auto)_1fr] gap-x-4 gap-y-1 text-hud-sub" data-figure="lab-event-tape">
            <dt className="text-muted-foreground">{t('lab.events.detail.tapeHash')}</dt>
            <dd className="font-mono" title={d.tape.content_sha256 ?? undefined}>{shortHash(d.tape.content_sha256)}</dd>
            <dt className="text-muted-foreground">{t('lab.events.detail.tapeActors')}</dt><dd className="font-mono">{fmt.count(d.tape.n_actors)}</dd>
            <dt className="text-muted-foreground">{t('lab.events.detail.tapeTicks')}</dt><dd className="font-mono">{fmt.count(d.tape.n_ticks)}</dd>
            <dt className="text-muted-foreground">{t('lab.events.detail.tapeBytes')}</dt><dd className="font-mono">{d.tape.bytes ? fmt.bytes(d.tape.bytes) : '—'}</dd>
          </dl>
        )}
      </div>
      <dl className="grid grid-cols-[minmax(8rem,auto)_1fr] gap-x-4 text-hud-sub" data-event-published={d.published ? '' : undefined}>
        <dt className="text-muted-foreground">{t('lab.events.detail.published')}</dt>
        <dd>{d.published ? t('lab.events.detail.publishedValue', { by: sanitizeText(d.published.by ?? '—', 48), at: fmtUnixNs(d.published.published_unix_ns) }) : t('lab.events.detail.notPublished')}</dd>
      </dl>
      {d.artifacts.length ? (
        <div>
          <h3 className="mb-1 text-hud-title font-semibold">{t('lab.events.detail.artifacts')}</h3>
          <ul className="grid grid-cols-[1fr_auto] gap-x-4 font-mono text-hud-sub">
            {d.artifacts.map((a) => <React.Fragment key={a.name}><li>{sanitizeText(a.name, 64)}</li><li className="text-right">{fmt.bytes(a.bytes)}</li></React.Fragment>)}
          </ul>
        </div>
      ) : null}
    </div>
  )
}
