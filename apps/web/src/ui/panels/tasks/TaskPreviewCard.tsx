// Preview result card of the "new task" page (R-D2-04, R-D2-05, R-D2-19; M15-FR-214; UX-FR-128, UX-FR-129; AWR-14 §20.9.3):
// the dry-run of stores/tasking.ts drawn by the planning layer (dashed: preview) and summarised here: vehicles and
// completion against T_max (LfStat), "why n vehicles", the scan geometry, the single-station footprint bound, coverage,
// the completion-time-versus-vehicles curve (LfLine with the T_max reference line, the chosen n as the hero point), the
// assignment table (LfTable), the 7x24 sustainability verdict with its bottlenecks and infeasible windows, and the
// warnings. An infeasible plan (422: 580, 581, 583, 585, 110) shows its reason, the localised `why` and the server's
// remedies; "fleet short" offers "add vehicle". Skeleton while pending (transitions.dev #43).
import type { TaskingTaskPreview } from '@awr/contracts/types'
import * as React from 'react'
import { reasonText, tOr, useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { rtClient } from '@/net/rt'
import { useTasking, whyKey, type TaskType } from '@/stores/tasking'
import { Alert, AlertAction, AlertDescription, AlertTitle } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import { LfLine } from '@/ui/lf/LfLine'
import { LfStat } from '@/ui/lf/LfStat'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { fleetDialogs } from '@/ui/panels/fleet/fleetDialogs'
import { entities } from '@/ui/sandbox/entities'
import { itemsText, mmss, previewSummary, warningKey } from './taskModel'

/** the model of a vehicle from the roster (assignment KPI "2 x P600") */
export function modelOfVehicle(uav: string): string | null {
  const rt = rtClient()
  if (!rt) return null
  return rt.roster.get(rt.roster.agentNoOf(uav))?.model ?? null
}

type Assignment = TaskingTaskPreview['assignments'][number]

function Infeasible({ code, detail }: { code: number; detail: Record<string, unknown> | null }) {
  const t = useT()
  const why = detail?.why
  const remedies = Array.isArray(detail?.remedies) ? (detail.remedies as { kind?: string; text_zh?: string }[]) : []
  const short = detail?.why === 'fleet_short'
  const nReq = typeof detail?.n_required === 'number' ? detail.n_required : null
  const lack = typeof detail?.shortfall === 'number' ? detail.shortfall : null
  return (
    <Alert className="border-dashed" data-task-infeasible={code}>
      <Icon icon="alert.warning" />
      <AlertTitle>{code ? `${reasonText(code).short} (${code})` : t('task.preview.failed')}</AlertTitle>
      <AlertDescription className="flex flex-col gap-1">
        {typeof why === 'string' ? <span data-task-why={why}>{tOr(whyKey(code, why), why)}</span> : null}
        {short && nReq !== null ? <span>{t('task.preview.fleetShort', { need: nReq, lack: lack ?? '-' })}</span> : null}
        {remedies.map((r, i) => <span key={i} className="text-muted-foreground">{sanitizeText(r.text_zh ?? r.kind ?? '', 120)}</span>)}
      </AlertDescription>
      {short ? (
        <AlertAction>
          <Button size="xs" variant="outline" onClick={() => {
            entities.show('drones')
            fleetDialogs.openAdd()
          }} data-task-add-vehicle="">{t('fleet.add')}</Button>
        </AlertAction>
      ) : null}
    </Alert>
  )
}

function Sustain({ s }: { s: NonNullable<TaskingTaskPreview['sustainability']> }) {
  const t = useT()
  const bad = (s.windows ?? []).filter((w) => !w.feasible)
  return (
    <div className="flex flex-col gap-1 text-hud-sub" data-task-sustain={s.verdict}>
      <span className="flex items-center gap-1.5">
        <span className="text-muted-foreground">{t('task.preview.sustain')}</span>
        <Badge variant={s.verdict === 'yes' ? 'secondary' : 'outline'} className={s.verdict === 'no' ? 'border-dashed' : undefined}>{t(`task.sustain.${s.verdict}`)}</Badge>
        {typeof s.n_air === 'number' ? <span className="ml-auto font-mono text-muted-foreground">{t('task.preview.nAir', { n: s.n_air, rec: s.n_air_recommended ?? s.n_air })}</span> : null}
      </span>
      {(s.bottlenecks ?? []).length ? (
        <span className="flex flex-wrap gap-1">
          {(s.bottlenecks ?? []).map((b) => <Badge key={b.code} variant="outline" className="border-dashed" data-task-bottleneck={b.code}>{tOr(`tasking.why.${b.code}`, b.code)}</Badge>)}
        </span>
      ) : null}
      {bad.length ? (
        <span className="flex flex-col gap-0.5 font-mono text-muted-foreground" data-task-windows={bad.length}>
          {bad.slice(0, 4).map((w) => (
            <span key={w.t_from_unix_ns}>{`${fmt.wallTime(Number(BigInt(w.t_from_unix_ns) / 1_000_000n))} - ${fmt.wallTime(Number(BigInt(w.t_to_unix_ns) / 1_000_000n))} · ${t(`sandbox.band.${w.band}`)} · ${w.why ? tOr(`tasking.why.${w.why}`, w.why) : ''}`}</span>
          ))}
        </span>
      ) : null}
    </div>
  )
}

export function TaskPreviewCard({ type }: { type: TaskType }) {
  const t = useT()
  const pv = useTasking((s) => s.preview)
  if (!pv) return null
  if (pv.state === 'pending') {
    return (
      <Card size="sm" data-task-preview-card="pending">
        <CardHeader><CardTitle className="flex items-center gap-1.5 text-hud-sub"><Icon icon="state.hold" />{t('task.preview.planning')}</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-2"><Skeleton className="h-8 w-full" /><Skeleton className="h-24 w-full" /></CardContent>
      </Card>
    )
  }
  if (pv.state !== 'done' || !pv.preview) return <Infeasible code={pv.code} detail={pv.detail} />
  const hCap = typeof pv.spec.params.h_cap_m === 'number' ? pv.spec.params.h_cap_m : 120
  return <PreviewBody type={type} p={pv.preview} hCapM={hCap} />
}

function PreviewBody({ type, p, hCapM }: { type: TaskType; p: TaskingTaskPreview; hCapM: number }) {
  const t = useT()
  const s = React.useMemo(() => previewSummary(p, modelOfVehicle), [p])
  const kindLabel = React.useCallback((k: string) => tOr(`task.item.${k}`, k), [])
  const columns: LfColumn<Assignment>[] = [
    { key: 'uav', label: t('task.col.uav'), format: (a) => <span className="font-mono">{sanitizeText(a.uav, 24)}</span> },
    { key: 'nest', label: t('task.col.nest'), format: (a) => <span className="font-mono">{a.nest ? sanitizeText(a.nest, 24) : '-'}</span> },
    { key: 'items', label: t('task.col.items'), format: (a) => <span className="truncate">{itemsText(a.items, kindLabel)}</span> },
    { key: 'eta', label: t('task.col.eta'), align: 'right', format: (a) => <span className="tabular-nums">{mmss(a.eta_s)}</span> },
  ]
  const curve = s.curve
  const heroIdx = curve.findIndex((c) => c.n === s.n)
  const scan = p.scan
  const cov = p.coverage
  return (
    <Card size="sm" data-task-preview-card="done" data-task-preview-n={s.n}>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5 text-hud-sub">
          <Icon icon="layer.visible" />
          {t('task.preview.title', { type: t(`task.type.${type}`) })}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-3 gap-2">
          <LfStat label={t('task.preview.n')} value={s.n} format={(v) => fmt.num(v, 0)} cls="D" />
          <LfStat label={t('task.preview.makespan')} unit="min" value={s.makespanMin ?? Number.NaN} format={(v) => (Number.isFinite(v) ? fmt.num(v, 1) : '-')} />
          <LfStat label={t('task.preview.tMax')} unit="min" value={s.tMaxMin ?? Number.NaN} format={(v) => (Number.isFinite(v) ? fmt.num(v, 1) : '-')} />
        </div>
        <p className="text-hud-sub" data-task-why-n={s.why?.rule ?? ''}>
          {t('task.preview.dispatch', { n: s.n, models: s.models.join(' / ') || t('task.preview.anyModel'), nests: s.nests.join(', ') || '-' })}
          {s.why?.rule === 'makespan' && s.why.nMinus1Min !== null ? ` · ${t('task.why.n', { n: s.n, n_minus_1: s.n - 1, t: fmt.num(s.why.nMinus1Min, 1) })}` : ''}
          {s.why?.rule === 'fleet_short' ? ` · ${t('task.why.fleetShort')}` : ''}
          {s.why?.rule === 'user_fixed' ? ` · ${t('task.why.userFixed')}` : ''}
        </p>
        {scan ? (
          <p className="font-mono text-hud-sub text-muted-foreground" data-task-scan="">
            {t('task.preview.scan', { h: fmt.num(scan.h_m ?? Number.NaN, 0), f: fmt.num(scan.f_mm ?? Number.NaN, 1), w: fmt.num(scan.strip_w_m ?? Number.NaN, 1),
              s: fmt.num(scan.spacing_m ?? Number.NaN, 1), n: scan.n_strips ?? '-' })}
          </p>
        ) : null}
        {scan?.footprint_bound_m ? (
          <p className="text-hud-sub text-muted-foreground" data-task-footprint="">
            {t('task.footprint', { d: fmt.num(scan.footprint_bound_m, 0), h: fmt.num(hCapM, 0), theta: fmt.num(scan.theta_e_deg ?? 15, 0) })}
          </p>
        ) : null}
        {cov ? (
          <p className="font-mono text-hud-sub text-muted-foreground" data-task-coverage="">
            {t('task.preview.coverage', { direct: fmt.num(cov.direct_pct ?? Number.NaN, 1), fills: cov.fill_waypoints ?? 0, obstacle: fmt.num(cov.obstacle_pct ?? 0, 2) })}
          </p>
        ) : null}
        {curve.length >= 2 ? (
          <div className="flex flex-col gap-1" data-task-curve={curve.length}>
            <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('task.preview.curve')}</span>
            <LfLine mode="static" data={curve.map((c) => ({ t: c.n, v: c.min }))} hero="index" heroIndex={heroIdx} peaks={0}
              refLine={s.tMaxMin !== null ? { v: s.tMaxMin, label: t('task.preview.tMaxRef', { t: fmt.num(s.tMaxMin, 1) }) } : undefined}
              xLabels={curve.map((c) => `n=${c.n}`)} height={120} format={(v) => fmt.num(v, 1)} ariaLabel={t('task.preview.curve')} />
          </div>
        ) : null}
        {p.assignments.length ? (
          <LfTable columns={columns} rows={p.assignments} rowKey={(a) => a.uav} ariaLabel={t('task.preview.assignments')} figureId="task-preview-assign"
            height={Math.min(160, 32 + 28 * p.assignments.length)} />
        ) : null}
        {p.sustainability ? <Sustain s={p.sustainability} /> : null}
        {(p.warnings ?? []).length ? (
          <span className="flex flex-col gap-0.5 text-hud-sub text-muted-foreground" data-task-preview-warnings="">
            {(p.warnings ?? []).map((w) => {
              const k = warningKey(w)
              return <span key={w} className="flex items-center gap-1"><Icon icon="alert.info" />{tOr(k.key, w, { arg: k.arg ?? '' })}</span>
            })}
          </span>
        ) : null}
      </CardContent>
    </Card>
  )
}
