// /lab/runs，运行列表与状态（AWR-14 §21.6、§21.15.4 至 §21.15.6；BN-42 至 BN-45；M27 §6.3；D3-AC-51）。取代原 ui/lab/LabRunsPage.tsx。
// - 状态徽标：M27 状态机八态（目录运行显示清单状态并标注"目录运行"）；PREPARING 且 restart_count > 0 时副文"第 k 次重启"；FAILED 红描边，
//   最新一条 FAILED 行的状态单元为表格唯一的 hot 单元。
// - 进度：排队第 n 位与阻塞原因（blocked_by）；取消请求登记后显示 10 s 终止时限的剩余秒数（超时提示等待调度器）。
// - 原因：`code NAME` 等宽与短文案，悬停显示处理建议（remedy）。
// - 操作（DropdownMenu）：打开观测墙（有帧数据时）、查看运行状态（Sheet：尝试记录与状态迁移审计）、取消运行、重试运行；取消与重试经
//   AlertDialog 确认；多选后"取消所选运行（n）"。可执行的操作只取服务端按请求者收窄后的 `actions`（受邀成员与他人项目为只读）。
// - 列表 5 s 轮询（有未结束运行且页面可见时；否则 30 s）；筛选与分页写入 URL（event、xp、policy、state、ranked、cursor），`run` 打开状态侧栏。
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { tOr, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { navigate, updateSearch, type RouteProps } from '@/app/router/router'
import { sanitizeText } from '@/lib/sanitize'
import { BenchError, benchGet } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader,
  AlertDialogTitle,
} from '@/ui/components/ui/alert-dialog'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Checkbox } from '@/ui/components/ui/checkbox'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/ui/components/ui/dropdown-menu'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { fmtNum, METRICS } from '@/ui/lab/board'
import { useBenchSession } from '@/ui/lab/useBenchSession'
import { LabHeader, useDocTitle } from '@/ui/lab/worlds/LabHeader'
import { eventsQuery } from '@/ui/lab/worlds/EventsPage'
import { eventQuery, LAB_ID_RE, parseEventFilter, type EventItem } from '@/ui/lab/worlds/worlds'
import { cancelAll, requestCancel, requestRetry } from './actions'
import { RunDetailBody } from './RunDetail'
import { RunReason, RunStateBadge } from './RunCells'
import {
  cancellable, filterPatch, hasFilter, hotFailed, isActive, parseRunFilter, pollInterval, pruneSelection, RUN_STATES, runPhase,
  runsQuery, TERMINAL_STATES, whyOf, type Confirm, type RunItem, type RunsPage as RunsPageDoc,
} from './runs'

export const runsQueryKey = (q: string) => ['lab', 'runs', q] as const
const ACTIVE_STATES = RUN_STATES.filter((s) => !TERMINAL_STATES.has(s))

function kindKey(kind: string): string {
  return kind === 'capture' ? 'capture' : kind === 'bench' ? 'bench' : 'run'
}

function Progress({ r, nowNs }: { r: RunItem; nowNs: number }) {
  const t = useT()
  const p = runPhase(r, nowNs)
  if (p.kind === 'queued') {
    return (
      <span data-run-phase="queued">
        {p.position ? t('lab.runs.queued', { n: p.position }) : t('lab.runs.queuedUnknown')}
        {p.blocked.length ? <span className="text-muted-foreground">{` · ${t('lab.runs.blocked', { what: p.blocked.map((b) => sanitizeText(b, 24)).join(', ') })}`}</span> : null}
      </span>
    )
  }
  if (p.kind === 'cancelling') {
    return <span data-run-phase="cancelling" className={p.overdue ? 'text-brand-text' : undefined}>{p.overdue ? t('lab.runs.cancelOverdue') : t('lab.runs.cancelling', { s: p.remainS ?? 0 })}</span>
  }
  return p.kind === 'active' ? <span data-run-phase="active">{t('lab.runs.active')}</span> : <span className="text-muted-foreground">—</span>
}

/** 取消与重试（BN-44、BN-45）；结果以 Toast 告知，失败时显示原因码与 why */
export function useRunActions() {
  const t = useT()
  const qc = useQueryClient()
  const [busy, setBusy] = React.useState<ReadonlySet<string>>(new Set())
  const mark = (ids: readonly string[], on: boolean) => setBusy((b) => {
    const n = new Set(b)
    for (const id of ids) {
      if (on) n.add(id)
      else n.delete(id)
    }
    return n
  })
  const settle = (ids: readonly string[]) => {
    void qc.invalidateQueries({ queryKey: ['lab', 'runs'] })
    for (const id of ids) void qc.invalidateQueries({ queryKey: ['lab', 'run', id] })
  }
  const rejected = (id: string, x: unknown) => {
    if (!(x instanceof BenchError)) return notify(`lab.run.${id}`, 'warning', t('lab.unreachable'), t('lab.unreachableHint'))
    const why = whyOf(x.detail)
    const p = { id, code: x.code ?? x.status, name: x.reasonName ?? '', why }
    notify(`lab.run.${id}`, 'warning', why ? t('lab.runs.rejectedWhy', p) : t('lab.runs.rejected', p))
  }
  const run = async (id: string, op: () => Promise<string>) => {
    mark([id], true)
    try {
      const msg = await op()
      notify(`lab.run.${id}`, 'info', msg)
    } catch (x) {
      rejected(id, x)
    } finally {
      mark([id], false)
      settle([id])
    }
  }
  const cancel = (id: string) => run(id, async () => {
    await requestCancel(id)
    return t('lab.runs.cancelled', { id })
  })
  const retry = (id: string) => run(id, async () => t('lab.runs.retried', { id, n: await requestRetry(id) }))
  const cancelMany = async (ids: readonly string[]): Promise<void> => {
    mark(ids, true)
    const r = await cancelAll(ids)
    mark(ids, false)
    settle(ids)
    const bad = r.rejected.length
    notify('lab.runs.batch', bad ? 'warning' : 'info', bad ? t('lab.runs.batchPartial', { ok: r.ok.length, bad }) : t('lab.runs.batchDone', { n: r.ok.length }))
  }
  return { busy, cancel, retry, cancelMany }
}

export function RunsPage({ search }: RouteProps) {
  const t = useT()
  const qc = useQueryClient()
  const session = useBenchSession()
  useDocTitle(t('lab.runs.docTitle'))
  const f = parseRunFilter(search)
  const qs = runsQuery(f)
  const q = useQuery({ queryKey: runsQueryKey(qs), queryFn: () => benchGet<RunsPageDoc>(`/runs${qs}`), enabled: session !== null,
    refetchInterval: (query) => pollInterval(query.state.data?.items) })
  const allEvents = parseEventFilter(new URLSearchParams())
  const evs = useQuery({ queryKey: eventsQuery(allEvents), queryFn: () => benchGet<{ items: EventItem[]; total: number }>(`/events${eventQuery(allEvents)}`),
    enabled: session !== null, staleTime: 60_000 })
  const rows = React.useMemo(() => q.data?.items ?? [], [q.data])
  const [picked, setPicked] = React.useState<ReadonlySet<string>>(new Set())
  const sel = React.useMemo(() => pruneSelection(picked, rows), [picked, rows])
  const [confirm, setConfirm] = React.useState<Confirm | null>(null)
  const act = useRunActions()
  const rid = search.get('run')
  // 取消时限倒计时：有取消中的运行时每秒更新，否则取最近一次取数时刻
  const cancelling = rows.some((r) => isActive(r) && r.cancel !== null)
  const [tick, setTick] = React.useState(0)
  React.useEffect(() => {
    if (!cancelling) return
    const h = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(h)
  }, [cancelling])
  const nowNs = Math.max(tick, q.dataUpdatedAt) * 1e6
  const hot = hotFailed(rows)
  const toggle = (id: string, on: boolean) => setPicked((p) => {
    const n = new Set(pruneSelection(p, rows))
    if (on) n.add(id)
    else n.delete(id)
    return n
  })
  const openRun = (r: RunItem) => (r.has_frames ? navigate(`/lab/runs/${encodeURIComponent(r.run_id)}`) : updateSearch({ run: r.run_id }))
  const cols: LfColumn<RunItem>[] = [
    { key: 'select', label: t('lab.runs.col.select'), width: '2.5rem', format: (r) => (r.actions.cancel ? (
      <span onClick={(e) => e.stopPropagation()} className="inline-flex">
        <Checkbox checked={sel.has(r.run_id)} onCheckedChange={(c: boolean) => toggle(r.run_id, c)} aria-label={t('lab.runs.selectRow', { id: r.run_id })} data-run-select={r.run_id} />
      </span>
    ) : null) },
    { key: 'run', label: t('lab.runs.col.run'), format: (r) => (
      <span className="font-mono">{sanitizeText(r.run_id, 64)}{r.managed && r.attempt ? <span className="text-muted-foreground">{` ${t('lab.runs.attempt', { n: r.attempt })}`}</span> : null}</span>
    ) },
    { key: 'event', label: t('lab.runs.col.event'), format: (r) => <span className="font-mono">{sanitizeText(r.event_id, 64)}</span> },
    { key: 'kind', label: t('lab.runs.col.kind'), format: (r) => (r.kind ? tOr(`lab.runs.kind.${kindKey(r.kind)}`, r.kind) : '—') },
    { key: 'world', label: t('lab.runs.col.world'), format: (r) => <span className="font-mono">{r.world_id ? sanitizeText(r.world_id, 48) : '—'}</span> },
    { key: 'policy', label: t('lab.runs.col.policy'), format: (r) => <span className="font-mono">{r.policy ? sanitizeText(r.policy, 48) : '—'}</span> },
    { key: 'physics', label: t('lab.runs.col.physics'), format: (r) => <span className="font-mono">{r.physics ? sanitizeText(r.physics, 24) : '—'}</span> },
    { key: 'state', label: t('lab.runs.col.state'), format: (r) => <RunStateBadge r={r} /> },
    { key: 'progress', label: t('lab.runs.col.progress'), format: (r) => <Progress r={r} nowNs={nowNs} /> },
    { key: 'reason', label: t('lab.runs.col.reason'), format: (r) => <RunReason r={r.reason} /> },
    ...METRICS.map((k): LfColumn<RunItem> => ({
      key: k, label: t(`lab.board.metric.${k}`), align: 'right', format: (r) => <span className="font-mono">{fmtNum(r.evaluation?.components[k])}</span>,
    })),
    { key: 'valid', label: t('lab.runs.col.valid'), format: (r) => (r.kind !== 'bench' ? '—' : !r.evaluation ? t('lab.runs.valid.pending')
      : r.evaluation.ranked_valid ? t('lab.runs.valid.yes') : `${t('lab.runs.valid.no')} · ${r.evaluation.invalid_reasons.map((x) => sanitizeText(x, 32)).join(', ')}`) },
    { key: 'frames', label: t('lab.runs.col.frames'), format: (r) => (r.has_frames ? t('lab.runs.frames.yes') : t('lab.runs.frames.no')) },
    { key: 'actions', label: t('lab.runs.col.actions'), format: (r) => (
      <span onClick={(e) => e.stopPropagation()} className="inline-flex">
        <RowMenu r={r} busy={act.busy.has(r.run_id)} onAsk={setConfirm} />
      </span>
    ) },
  ]
  const refresh = () => void qc.invalidateQueries({ queryKey: ['lab', 'runs'] })
  const stateValue = f.state.length === 1 ? f.state[0] : f.state.length === ACTIVE_STATES.length && ACTIVE_STATES.every((s) => f.state.includes(s)) ? 'active' : ''
  const eventIds = [...new Set([...(evs.data?.items ?? []).map((e) => e.event_id), ...(f.event ? [f.event] : [])])].sort()
  const canSelect = cancellable(rows)
  return (
    <section data-view="lab-runs" data-figure="lab-runs" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <LabHeader page="runs" icon="data.folder" title={t('lab.runs.title')} session={session} onRefresh={refresh} />
      <div className="mb-3 flex flex-wrap items-end gap-3" data-figure="lab-runs-filter">
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.runs.filter.state')}</span>
          <NativeSelect size="sm" value={stateValue} data-runs-filter="state"
            onChange={(e) => updateSearch(filterPatch({ state: e.target.value === 'active' ? [...ACTIVE_STATES] : e.target.value ? [e.target.value] : [] }))}>
            <NativeSelectOption value="">{t('lab.runs.filter.allStates')}</NativeSelectOption>
            <NativeSelectOption value="active">{t('lab.runs.filter.active')}</NativeSelectOption>
            {RUN_STATES.map((s) => <NativeSelectOption key={s} value={s}>{t(`lab.runs.state.${s}`)}</NativeSelectOption>)}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.runs.filter.event')}</span>
          <NativeSelect size="sm" value={f.event ?? ''} data-runs-filter="event" onChange={(e) => updateSearch(filterPatch({ event: e.target.value || null }))}>
            <NativeSelectOption value="">{t('lab.runs.filter.allEvents')}</NativeSelectOption>
            {eventIds.map((id) => <NativeSelectOption key={id} value={id}>{id}</NativeSelectOption>)}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.runs.filter.ranked')}</span>
          <NativeSelect size="sm" value={f.ranked === null ? '' : String(f.ranked)} data-runs-filter="ranked"
            onChange={(e) => updateSearch(filterPatch({ ranked: e.target.value === '' ? null : e.target.value === 'true' }))}>
            <NativeSelectOption value="">{t('lab.runs.filter.rankedAll')}</NativeSelectOption>
            <NativeSelectOption value="true">{t('lab.runs.filter.rankedYes')}</NativeSelectOption>
            <NativeSelectOption value="false">{t('lab.runs.filter.rankedNo')}</NativeSelectOption>
          </NativeSelect>
        </label>
        {f.xp ? <Badge variant="outline" className="h-7" data-runs-chip="xp">{t('lab.runs.filter.experiment', { xp: sanitizeText(f.xp, 48) })}</Badge> : null}
        {f.policy ? <Badge variant="outline" className="h-7" data-runs-chip="policy">{t('lab.runs.filter.policy', { p: sanitizeText(f.policy, 48) })}</Badge> : null}
        {hasFilter(f) ? (
          <Button size="sm" variant="ghost" onClick={() => updateSearch(filterPatch({ event: null, xp: null, policy: null, state: [], ranked: null }))}>{t('lab.runs.filter.clear')}</Button>
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          {canSelect.length ? (
            <Button size="sm" variant="ghost" onClick={() => setPicked(new Set(canSelect.map((r) => r.run_id)))} data-runs-select-all="">{t('lab.runs.selectAll')}</Button>
          ) : null}
          <Button size="sm" variant="outline" disabled={!sel.size} onClick={() => setConfirm({ kind: 'batch', ids: [...sel] })} data-runs-batch-cancel={sel.size}>
            <Icon icon="mission.abort" data-icon="inline-start" />
            {t('lab.runs.batchCancel', { n: sel.size })}
          </Button>
          {q.data ? <span className="text-hud-sub text-muted-foreground">{t('lab.runs.total', { n: q.data.total })}</span> : null}
        </div>
      </div>
      {q.isPending ? (
        <div className="flex flex-col gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
      ) : q.isError ? (
        <PanelEmpty title={t('lab.unreachable')} description={t('lab.unreachableHint')}
          action={<Button size="sm" variant="outline" onClick={() => void q.refetch()}>{t('common.retry')}</Button>} />
      ) : !rows.length ? (
        hasFilter(f) ? <PanelEmpty title={t('lab.runs.noMatch')} /> : <PanelEmpty title={t('lab.runs.empty')} description={t('lab.runs.emptyHint')} />
      ) : (
        <>
          <LfTable columns={cols} rows={rows} rowKey={(r) => r.run_id} ariaLabel={t('lab.runs.title')} figureId="lab-runs-table"
            hot={hot ? { row: hot, col: 'state' } : null} selected={rid} onRowClick={openRun}
            height={Math.max(240, typeof window !== 'undefined' ? window.innerHeight - 200 : 480)} />
          {f.cursor || q.data?.next_cursor ? (
            <div className="mt-2 flex justify-end gap-2">
              <Button size="sm" variant="ghost" disabled={!f.cursor} onClick={() => updateSearch({ cursor: null })}>{t('lab.runs.firstPage')}</Button>
              <Button size="sm" variant="outline" disabled={!q.data?.next_cursor} onClick={() => updateSearch({ cursor: q.data?.next_cursor ?? null })}>{t('lab.runs.nextPage')}</Button>
            </div>
          ) : null}
        </>
      )}
      <Sheet open={rid !== null && LAB_ID_RE.test(rid)} onOpenChange={(o) => (o ? null : updateSearch({ run: null }))}>
        <SheetContent side="right" className="w-160 max-w-full overflow-auto sm:max-w-160" data-run-sheet={rid ?? ''}>
          <SheetHeader className="pb-0">
            <SheetTitle className="font-mono">{t('lab.runs.detail.title', { id: sanitizeText(rid ?? '', 64) })}</SheetTitle>
            <SheetDescription>{t('lab.runs.detail.description')}</SheetDescription>
          </SheetHeader>
          {rid && LAB_ID_RE.test(rid) ? <RunDetailBody rid={rid} enabled={session !== null} busy={act.busy.has(rid)} onAsk={setConfirm} /> : null}
        </SheetContent>
      </Sheet>
      <ConfirmDialog confirm={confirm} onClose={() => setConfirm(null)} onRun={(c) => {
        if (c.kind === 'batch') {
          setPicked(new Set())
          void act.cancelMany(c.ids)
        } else if (c.kind === 'cancel') void act.cancel(c.run.run_id)
        else void act.retry(c.run.run_id)
      }} />
    </section>
  )
}

/** 行操作菜单（§21.6"操作"列）：只呈现服务端允许的取消与重试 */
export function RowMenu({ r, busy, onAsk }: { r: RunItem; busy: boolean; onAsk: (c: Confirm) => void }) {
  const t = useT()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="icon-xs" variant="ghost" aria-label={t('lab.runs.rowMenu', { id: r.run_id })} data-run-menu={r.run_id} />}>
        <Icon icon="more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="font-mono">{sanitizeText(r.run_id, 64)}</DropdownMenuLabel>
          <DropdownMenuItem disabled={!r.has_frames} onClick={() => navigate(`/lab/runs/${encodeURIComponent(r.run_id)}`)}><Icon icon="sensor.camera" />{t('lab.runs.op.open')}</DropdownMenuItem>
          <DropdownMenuItem onClick={() => updateSearch({ run: r.run_id })}><Icon icon="data.json" />{t('lab.runs.op.detail')}</DropdownMenuItem>
          {r.actions.cancel || r.actions.retry ? <DropdownMenuSeparator /> : null}
          {r.actions.cancel ? (
            <DropdownMenuItem disabled={busy} data-run-cancel={r.run_id} onClick={() => onAsk({ kind: 'cancel', run: r })}><Icon icon="mission.abort" />{t('lab.runs.op.cancel')}</DropdownMenuItem>
          ) : null}
          {r.actions.retry ? (
            <DropdownMenuItem disabled={busy} data-run-retry={r.run_id} onClick={() => onAsk({ kind: 'retry', run: r })}><Icon icon="refresh" />{t('lab.runs.op.retry')}</DropdownMenuItem>
          ) : null}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** 取消、重试与批量取消的确认对话框（§21.15.5 消息模板；按钮"取消运行""重试运行""取消 n 个运行"与"返回"） */
export function ConfirmDialog({ confirm, onClose, onRun }: { confirm: Confirm | null; onClose: () => void; onRun: (c: Confirm) => void }) {
  const t = useT()
  const [shown, setShown] = React.useState<Confirm | null>(confirm)
  if (confirm && confirm !== shown) setShown(confirm)
  const c = confirm ?? shown
  const title = !c ? '' : c.kind === 'batch' ? t('lab.runs.batchTitle', { n: c.ids.length })
    : c.kind === 'cancel' ? t('lab.runs.cancelTitle', { id: c.run.run_id }) : t('lab.runs.retryTitle', { id: c.run.run_id })
  const body = !c ? '' : c.kind === 'batch' ? t('lab.runs.batchBody') : c.kind === 'cancel' ? t('lab.runs.cancelBody')
    : t('lab.runs.retryBody', { n: (c.run.attempt ?? 1) + 1 })
  const action = !c ? '' : c.kind === 'batch' ? t('lab.runs.batchAction', { n: c.ids.length }) : c.kind === 'cancel' ? t('lab.runs.op.cancel') : t('lab.runs.op.retry')
  return (
    <AlertDialog open={confirm !== null} onOpenChange={(o) => (o ? null : onClose())}>
      <AlertDialogContent data-run-confirm={c?.kind ?? ''}>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{body}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('lab.runs.back')}</AlertDialogCancel>
          <AlertDialogAction data-run-confirm-action={c?.kind ?? ''} onClick={() => {
            if (confirm) onRun(confirm)
            onClose()
          }}>{action}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
