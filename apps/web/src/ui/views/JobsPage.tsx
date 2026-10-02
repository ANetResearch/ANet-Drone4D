// Reconstruction Jobs overlay page, /jobs (AWR-14 §5.7, §7.2; M01 §8.2; D1-AC-22 UI part; D1-ext): the Mock
// reconstruction chain MockEngine -> Recon IR -> ingest -> World Package -> Web.
//   header    title, the chain in one line, "new job" (operator holding the seat), close;
//   table     table.log: job, engine, source and target world, state (icon + words, English stage in the Tooltip),
//             progress (Progress + percent + frames, <= 4 Hz through stores/jobs), scale_status badge (always shown:
//             "relative scale" outline with "scale unknown, distances and heights are not physical"; GNSS, RTK, LiDAR
//             secondary), start wall clock, elapsed, row actions (cancel, retry when resumable, open the product world);
//   detail    Sheet: stage strip (done grey, current = the figure's hero, not started = floor ticks), alignment summary
//             (method, inlier ratio, RMSE, gate, "needs review" as words), product world card with "simulated data"
//             and "open in the sandbox", log tail (monospace, <= 200 lines);
//   new job   Dialog: source world, synthetic path (helix, lawnmower), frames (default 600), target world id
//             (^[a-z0-9-]{1,63}$, empty = server default), GNSS georeferencing; 124/332/330 become field errors.
// When the job service is not wired (404) or job-worker is down (213) the page says so instead of an empty queue.
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { reasonText, useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { worldsQuery } from '@/app/query/options'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { sanitizeText } from '@/lib/sanitize'
import { ApiError } from '@/net/api'
import { notify } from '@/app/providers/ToastProvider'
import { Alert, AlertAction, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/ui/components/ui/field'
import { Input } from '@/ui/components/ui/input'
import { Progress } from '@/ui/components/ui/progress'
import { ScrollArea } from '@/ui/components/ui/scroll-area'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Slider } from '@/ui/components/ui/slider'
import { Switch } from '@/ui/components/ui/switch'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { PanelEmpty } from '@/ui/brand'
import { useConnView } from '@/ui/shell/connView'
import { writeDeniedKey } from '@/ui/shell/guards'
import { BUILD_STAGES, RECON_STAGES, TERMINAL, jobs, useJobs, type JobRow } from '@/stores/jobs'
import { useFrameCapOverlay } from './useFrameCapOverlay'

const TARGET_RE = /^[a-z0-9-]{1,63}$/
const LOG_SKIP = new Set(['t_wall_ns', 'job_id', 'level', 'msg'])

/** one job.log record (a JSON line from R64) as "12:56:54 INFO stage PREPARING done · seconds=2.21", sanitised */
export function logLine(raw: string): string {
  let rec: Record<string, unknown> | null = null
  try {
    const v: unknown = JSON.parse(raw)
    rec = v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
  } catch {
    rec = null
  }
  if (!rec) return sanitizeText(raw, 400)
  const ns = Number(rec.t_wall_ns)
  const when = Number.isFinite(ns) && ns > 0 ? fmt.wallTime(ns / 1e6) : ''
  const kv = Object.entries(rec).filter(([k]) => !LOG_SKIP.has(k)).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
  const lvl = typeof rec.level === 'string' ? rec.level.toUpperCase() : ''
  const msg = typeof rec.msg === 'string' ? rec.msg : ''
  return sanitizeText([when, lvl, msg, kv.length ? `· ${kv.join(' ')}` : ''].filter(Boolean).join(' '), 400)
}

/** icon of a job state (static; states change by the 04 text swap of the label) */
export function jobIcon(r: Pick<JobRow, 'state' | 'pausedReason'>): IconKey {
  if (r.pausedReason) return 'mission.paused'
  if (r.state === 'SUCCEEDED') return 'mission.done'
  if (r.state === 'FAILED') return 'mission.failed'
  if (r.state === 'CANCELLED') return 'mission.abort'
  if (r.state === 'QUEUED') return 'mission.pending'
  return 'mission.running'
}

/** i18n key of the state words (paused by the performance lock wins) */
export const jobStateKey = (r: Pick<JobRow, 'state' | 'pausedReason'>): string => (r.pausedReason === 'perf_lock' ? 'jobs.state.pausedPerf' : `jobs.state.${r.state}`)

/** scale_status badge: relative is an outline with the physical warning, measured scales are secondary (M01 §8.2) */
export function ScaleBadge({ scale }: { scale: string | null }) {
  const t = useT()
  if (!scale) return <span className="text-muted-foreground">—</span>
  const rel = scale === 'relative'
  return (
    <Tooltip>
      <TooltipTrigger render={<span className="inline-flex" />}>
        <Badge variant={rel ? 'outline' : 'secondary'} data-scale-status={scale}>{t(`jobs.scale.${scale}`)}</Badge>
      </TooltipTrigger>
      <TooltipContent>{t(rel ? 'jobs.scale.relativeHint' : `jobs.scale.${scale}.full`)}</TooltipContent>
    </Tooltip>
  )
}

function elapsedS(r: JobRow): number {
  const end = TERMINAL.has(r.state) ? r.updatedMs : Date.now()
  return Number.isFinite(r.createdMs) && Number.isFinite(end) ? Math.max(0, (end - r.createdMs) / 1000) : Number.NaN
}

function ProgressCell({ r }: { r: JobRow }) {
  const t = useT()
  const frames = r.framesTotal !== null && r.framesDone !== null && r.stage === 'INFERRING'
    ? (r.fps !== null && r.fps > 0 ? t('jobs.frames', { done: fmt.count(r.framesDone), total: fmt.count(r.framesTotal), fps: fmt.num(r.fps, 1) })
      : t('jobs.framesOnly', { done: fmt.count(r.framesDone), total: fmt.count(r.framesTotal) })) : null
  return (
    <div className="flex min-w-40 flex-col gap-1" data-job-progress={Math.round(r.progressPct)}>
      <div className="flex items-center gap-2">
        <Progress value={r.progressPct} className="flex-1" aria-label={t('jobs.col.progress')} />
        <span className="w-10 text-right font-mono text-hud-sub tabular-nums">{fmt.pct(r.progressPct)}</span>
      </div>
      {frames || (r.etaS !== null && !TERMINAL.has(r.state)) ? (
        <span className="text-hud-cap text-muted-foreground tabular-nums">
          {[frames, r.etaS !== null && !TERMINAL.has(r.state) ? t('jobs.eta', { s: fmt.dur(r.etaS) }) : null].filter(Boolean).join(' · ')}
        </span>
      ) : null}
    </div>
  )
}

function StateCell({ r }: { r: JobRow }) {
  const t = useT()
  const failed = r.state === 'FAILED'
  return (
    <Tooltip>
      <TooltipTrigger render={<span className={cn('inline-flex items-center gap-1.5', failed && 'text-brand-text', r.state === 'CANCELLED' && 'text-muted-foreground')} data-job-state={r.state} />}>
        <Icon icon={jobIcon(r)} />
        {t(jobStateKey(r))}
      </TooltipTrigger>
      <TooltipContent className="font-mono">{r.stage ?? r.state}</TooltipContent>
    </Tooltip>
  )
}

function RowActions({ r, canWrite }: { r: JobRow; canWrite: boolean }) {
  const t = useT()
  const act = (p: Promise<void>, key: string) => void p.catch((e: unknown) => notify(`jobs:${key}`, 'warning', e instanceof ApiError && e.reason ? reasonText(e.reason).short : t('jobs.actionFailed')))
  return (
    <span className="inline-flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
      {!TERMINAL.has(r.state) ? (
        <Button size="xs" variant="ghost" disabled={!canWrite} onClick={() => act(jobs.cancel(r.jobId), 'cancel')} data-job-cancel={r.jobId}>{t('jobs.cancel')}</Button>
      ) : null}
      {r.state === 'FAILED' && r.resumable ? (
        <Button size="xs" variant="ghost" disabled={!canWrite} onClick={() => act(jobs.retry(r.jobId), 'retry')}>{t('jobs.retry')}</Button>
      ) : null}
      {r.state === 'SUCCEEDED' && r.outputWorldId ? (
        <Button size="xs" variant="outline" onClick={() => navigate(`/world/${r.outputWorldId}`)} data-job-open={r.outputWorldId}>{t('jobs.openWorld')}</Button>
      ) : null}
    </span>
  )
}

/** stage strip of M01 §8.2 / AWR-14 §5.7: done solid grey, current the figure's hero, not started floor ticks */
function StageStrip({ r }: { r: JobRow }) {
  const t = useT()
  const stages = ['QUEUED', ...(r.kind === 'world_build' ? BUILD_STAGES : RECON_STAGES)]
  const cur = r.state === 'SUCCEEDED' ? stages.length : stages.indexOf(r.state === 'FAILED' || r.state === 'CANCELLED' ? r.stage ?? 'QUEUED' : r.state)
  return (
    <div className="flex flex-col gap-1.5" data-figure="job-stages">
      <div className="flex gap-1">
        {stages.map((s, i) => (
          <span key={s} aria-hidden="true" className={cn('h-2 flex-1 rounded-xs', i < cur ? 'bg-lf-faintdata' : i === cur ? (r.state === 'CANCELLED' ? 'bg-muted-foreground' : 'bg-lf-hero') : 'border-b border-lf-grid')} />
        ))}
      </div>
      <div className="flex gap-1">
        {stages.map((s, i) => (
          <span key={s} className={cn('min-w-0 flex-1 text-hud-cap leading-tight', i === cur ? 'font-semibold text-foreground' : 'text-muted-foreground')} title={s}>{t(`jobs.state.${s}`)}</span>
        ))}
      </div>
    </div>
  )
}

function JobSheet({ r, onClose }: { r: JobRow | null; onClose: () => void }) {
  const t = useT()
  const [log, setLog] = React.useState<string[] | null>(null)
  const id = r?.jobId ?? null
  const state = r?.state ?? null
  const [logFor, setLogFor] = React.useState<string | null>(id)
  if (logFor !== id) {
    setLogFor(id)
    setLog(null)
  }
  React.useEffect(() => {
    if (!id) return
    let alive = true
    const load = () => void jobs.log(id).then((l) => alive && setLog(l), () => alive && setLog([]))
    load()
    const h = state && !TERMINAL.has(state) ? setInterval(load, 2000) : null
    return () => {
      alive = false
      if (h) clearInterval(h)
    }
  }, [id, state])
  const a = r?.alignment
  return (
    <Sheet open={r !== null} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-xl" data-job-sheet={id ?? ''}>
        {r ? (
          <>
            <SheetHeader>
              <SheetTitle className="flex items-center gap-2 font-mono">
                {r.jobId}
                {a?.needsReview ? <span className="font-sans text-hud-sub text-muted-foreground">{t('jobs.needsReview')}</span> : null}
              </SheetTitle>
              <SheetDescription>{t('jobs.sheet.sub', { engine: r.engine ?? 'mock', src: r.sourceWorldId ?? '—', dst: r.targetWorldId ?? '—' })}</SheetDescription>
            </SheetHeader>
            <ScrollArea className="min-h-0 flex-1">
              <div className="flex flex-col gap-5 px-4 pb-6">
                <StageStrip r={r} />
                <div className="flex items-center gap-3">
                  <StateCell r={r} />
                  <div className="flex-1"><ProgressCell r={r} /></div>
                </div>
                {r.error ? (
                  <Alert data-job-error={r.error.code}>
                    <Icon icon="alert.error" />
                    <AlertDescription>{`${r.error.code} ${r.error.name ?? ''} · ${reasonText(r.error.code).short}${r.error.message ? ` · ${sanitizeText(r.error.message, 200)}` : ''}`}</AlertDescription>
                  </Alert>
                ) : null}
                <section className="flex flex-col gap-2">
                  <h3 className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('jobs.sheet.scale')}</h3>
                  <div className="flex flex-wrap items-center gap-2">
                    <ScaleBadge scale={r.scaleStatus} />
                    {r.scaleStatus === 'relative' ? <span className="text-hud-sub text-muted-foreground">{t('jobs.scale.relativeHint')}</span> : null}
                  </div>
                  {a ? (
                    <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-hud-sub">
                      <dt className="text-muted-foreground">{t('jobs.align.method')}</dt><dd className="font-mono">{a.method ?? '—'}</dd>
                      <dt className="text-muted-foreground">{t('jobs.align.inliers')}</dt><dd className="font-mono">{a.inlierRatio !== null ? fmt.pct(a.inlierRatio * 100, 1) : '—'}</dd>
                      <dt className="text-muted-foreground">{t('jobs.align.rmse')}</dt><dd className="font-mono">{a.rmseM !== null ? `${fmt.num(a.rmseM, 2)} m` : '—'}</dd>
                      <dt className="text-muted-foreground">{t('jobs.align.gate')}</dt><dd>{a.status ? t(`jobs.gate.${a.status}`) : '—'}</dd>
                    </dl>
                  ) : null}
                </section>
                {r.state === 'SUCCEEDED' && r.outputWorldId ? (
                  <section className="flex items-center justify-between gap-3 rounded-lg p-3 ring-1 ring-foreground/10" data-job-world={r.outputWorldId}>
                    <div className="flex min-w-0 flex-col gap-1">
                      <span className="flex items-center gap-2">
                        <Icon icon="nav.world" />
                        <span className="truncate font-mono">{r.outputWorldId}</span>
                        {r.engine === 'mock' ? <Badge variant="outline">{t('jobs.simulatedData')}</Badge> : null}
                      </span>
                      <span className="text-hud-sub text-muted-foreground">{t('jobs.product')}</span>
                    </div>
                    <Button size="sm" onClick={() => navigate(`/world/${r.outputWorldId}`)}>{t('jobs.openInSandbox')}</Button>
                  </section>
                ) : null}
                <section className="flex flex-col gap-2">
                  <h3 className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('jobs.sheet.log')}</h3>
                  {log === null ? <Skeleton className="h-24 rounded-md" /> : log.length ? (
                    <pre className="max-h-96 overflow-auto rounded-md bg-muted p-2 font-mono text-hud-cap leading-relaxed whitespace-pre-wrap" data-job-log="">{log.slice(-200).map(logLine).join('\n')}</pre>
                  ) : <p className="text-hud-sub text-muted-foreground">{t('jobs.noLog')}</p>}
                </section>
              </div>
            </ScrollArea>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  )
}

function NewJobDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const t = useT()
  const worlds = useQuery(worldsQuery())
  const ready = (worlds.data ?? []).filter((w) => (w.status ?? 'ready') === 'ready')
  const [src, setSrc] = React.useState('shenzhen')
  const [path, setPath] = React.useState<'helix' | 'lawnmower'>('helix')
  const [frames, setFrames] = React.useState(600)
  const [target, setTarget] = React.useState('')
  const [gnss, setGnss] = React.useState(true)
  const [busy, setBusy] = React.useState(false)
  const [err, setErr] = React.useState<{ field: 'target' | 'form'; text: string } | null>(null)
  const targetBad = target !== '' && !TARGET_RE.test(target)
  const submit = async () => {
    if (targetBad) return
    setBusy(true)
    setErr(null)
    try {
      const id = await jobs.submitRecon({ sourceWorldId: src, path, frames, targetWorldId: target || undefined, georef: gnss ? 'gnss' : 'none' })
      notify('jobs:submitted', 'info', t('jobs.submitted', { id }))
      onOpenChange(false)
    } catch (e) {
      const code = e instanceof ApiError ? e.reason : null
      const text = code ? `${code} ${reasonText(code).short}` : t('jobs.actionFailed')
      setErr({ field: code === 124 || code === 332 ? 'target' : 'form', text })
    } finally {
      setBusy(false)
    }
  }
  const srcItems = (ready.length ? ready : [{ id: 'shenzhen' }]).map((w) => ({ value: w.id, label: w.id }))
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg" data-job-new="">
        <DialogHeader>
          <DialogTitle>{t('jobs.new.title')}</DialogTitle>
          <DialogDescription>{t('jobs.new.desc')}</DialogDescription>
        </DialogHeader>
        <FieldGroup>
          <Field>
            <FieldLabel>{t('jobs.new.source')}</FieldLabel>
            <Select items={srcItems} value={src} onValueChange={(v) => setSrc(String(v))}>
              <SelectTrigger className="w-full font-mono"><SelectValue /></SelectTrigger>
              <SelectContent>{srcItems.map((w) => <SelectItem key={w.value} value={w.value} className="font-mono">{w.label}</SelectItem>)}</SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel>{t('jobs.new.path')}</FieldLabel>
            <Select items={[{ value: 'helix', label: t('jobs.path.helix') }, { value: 'lawnmower', label: t('jobs.path.lawnmower') }]} value={path} onValueChange={(v) => setPath(v as 'helix' | 'lawnmower')}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="helix">{t('jobs.path.helix')}</SelectItem>
                <SelectItem value="lawnmower">{t('jobs.path.lawnmower')}</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel className="justify-between">{t('jobs.new.frames')}<span className="font-mono tabular-nums">{fmt.count(frames)}</span></FieldLabel>
            <Slider value={[frames]} min={60} max={1200} step={60} onValueChange={(v: number | readonly number[]) => setFrames(Array.isArray(v) ? (v as number[])[0] : (v as number))} aria-label={t('jobs.new.frames')} />
          </Field>
          <Field data-invalid={targetBad || err?.field === 'target' ? true : undefined}>
            <FieldLabel htmlFor="job-target">{t('jobs.new.target')}</FieldLabel>
            <Input id="job-target" className="font-mono" value={target} placeholder={`${src}-recon-01`} maxLength={63} aria-invalid={targetBad || err?.field === 'target'}
              onChange={(e) => setTarget(e.target.value.trim())} />
            {targetBad ? <FieldError>{t('jobs.new.targetBad')}</FieldError> : err?.field === 'target' ? <FieldError>{err.text}</FieldError> : <FieldDescription>{t('jobs.new.targetHint')}</FieldDescription>}
          </Field>
          <Field orientation="horizontal" className="justify-between">
            <FieldLabel htmlFor="job-gnss">{t('jobs.new.gnss')}</FieldLabel>
            <Switch id="job-gnss" checked={gnss} onCheckedChange={(v: boolean) => setGnss(v)} />
          </Field>
          {err?.field === 'form' ? <FieldError>{err.text}</FieldError> : null}
        </FieldGroup>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button disabled={busy || targetBad} onClick={() => void submit()} data-job-submit="">{t('jobs.new.submit')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function JobsPage() {
  const t = useT()
  useFrameCapOverlay('jobs')
  const rows = useJobs((s) => s.jobs)
  const status = useJobs((s) => s.status)
  const code = useJobs((s) => s.code)
  useConnView((s) => s.version)
  const denied = writeDeniedKey()
  const canWrite = denied === null
  const [open, setOpen] = React.useState<string | null>(null)
  const [creating, setCreating] = React.useState(false)
  React.useEffect(() => {
    void jobs.refresh()
    const h = setInterval(() => void jobs.refresh(), 15_000)
    return () => clearInterval(h)
  }, [])
  const failedHot = rows.find((r) => r.state === 'FAILED')
  const cols: LfColumn<JobRow>[] = [
    { key: 'id', label: t('jobs.col.job'), format: (r) => <span className="font-mono" title={r.jobId}>{r.jobId.length > 14 ? `${r.jobId.slice(0, 6)}…${r.jobId.slice(-6)}` : r.jobId}</span> },
    { key: 'engine', label: t('jobs.col.engine'), format: (r) => (r.engine ? t(`jobs.engine.${r.engine}`) : r.kind === 'world_build' ? t('jobs.kind.world_build') : '—') },
    { key: 'src', label: t('jobs.col.source'), format: (r) => <span className="font-mono">{r.sourceWorldId ?? '—'}</span> },
    { key: 'dst', label: t('jobs.col.target'), format: (r) => <span className="font-mono">{r.targetWorldId ?? '—'}</span> },
    { key: 'state', label: t('jobs.col.state'), format: (r) => <StateCell r={r} /> },
    { key: 'progress', label: t('jobs.col.progress'), width: '16rem', format: (r) => <ProgressCell r={r} /> },
    { key: 'scale', label: t('jobs.col.scale'), format: (r) => <ScaleBadge scale={r.scaleStatus} /> },
    { key: 'start', label: t('jobs.col.start'), format: (r) => <span className="font-mono tabular-nums">{fmt.wallTime(r.createdMs)}</span> },
    { key: 'elapsed', label: t('jobs.col.elapsed'), align: 'right', format: (r) => fmt.dur(elapsedS(r)) },
    { key: 'act', label: '', align: 'right', format: (r) => <RowActions r={r} canWrite={canWrite} /> },
  ]
  const current = rows.find((r) => r.jobId === open) ?? null
  const newBtn = (
    <Button size="sm" disabled={!canWrite} focusableWhenDisabled className="aria-disabled:opacity-50" onClick={() => setCreating(true)} data-job-new-open="">
      <Icon icon="plus" data-icon="inline-start" />
      {t('jobs.new.open')}
    </Button>
  )
  return (
    <section data-view="jobs" data-figure="jobs" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon icon="nav.recon" />
        <h1 className="text-ed-title font-bold">{t('jobs.title')}</h1>
        <span className="hidden text-hud-sub text-muted-foreground lg:inline">{t('jobs.subtitle')}</span>
        <div className="ml-auto flex items-center gap-2">
          {canWrite ? newBtn : (
            <Tooltip>
              <TooltipTrigger render={<span className="inline-flex" />}>{newBtn}</TooltipTrigger>
              <TooltipContent>{t(denied ?? 'hint.readOnly')}</TooltipContent>
            </Tooltip>
          )}
          <Button size="sm" variant="ghost" onClick={() => (history.length > 1 ? history.back() : navigate('/'))}>
            <Icon icon="close" data-icon="inline-start" />
            {t('common.close')}
          </Button>
        </div>
      </div>
      {status === 'unavailable' || status === 'error' ? (
        <Alert className="mb-3" data-jobs-unavailable={code ?? ''}>
          <Icon icon="alert.info" />
          <AlertDescription>{code === 213 ? t('jobs.workerDown') : code === 404 ? t('jobs.notWired') : t('jobs.loadFailed', { code: code ?? '—' })}</AlertDescription>
          <AlertAction><Button size="xs" variant="outline" onClick={() => void jobs.refresh()}>{t('common.retry')}</Button></AlertAction>
        </Alert>
      ) : null}
      {status === 'loading' || (status === 'idle' && !rows.length) ? (
        <div className="flex flex-col gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-10 rounded-md" />)}</div>
      ) : !rows.length ? (
        <PanelEmpty title={t('jobs.empty')} description={t('jobs.emptyHint')} action={canWrite ? <Button size="sm" variant="outline" onClick={() => setCreating(true)}>{t('jobs.new.open')}</Button> : undefined} />
      ) : (
        <LfTable columns={cols} rows={rows} rowKey={(r) => r.jobId} rowHeight={44} onRowClick={(r) => setOpen(r.jobId)} selected={open}
          hot={failedHot ? { row: failedHot.jobId, col: 'state' } : null} height={Math.max(240, typeof window !== 'undefined' ? window.innerHeight - 140 : 480)}
          ariaLabel={t('jobs.title')} figureId="jobs-table" />
      )}
      <JobSheet r={current} onClose={() => setOpen(null)} />
      <NewJobDialog open={creating} onOpenChange={setCreating} />
    </section>
  )
}
