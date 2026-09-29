// Test report page (M15-FR-080; AWR-18 §11.2, §11.4; M16-FR-071): renders an `awr.perf.report.v1` JSON on the light
// paper theme (`.dark` removed while the page is mounted; ADR-032 light theme only for reports) with editorial-density
// lieflat components: cover (badge 480 px), AT A GLANCE (P0 passed / total, Shenzhen full-scene p95, the largest TTFP,
// the 1000-vehicle sim-core CPU), the gate table (one row per case, status in words with an icon, the key metric
// against its threshold; the most severe P0 failure is the table's one hot cell), frame rhythm and TTFP ranks (F1 / F5
// static SVG), baseline comparison, environment fingerprint and the appendix of every metric. Sources: `/reports?src=`
// (same-origin relative path, M16 render.mjs injects it with page.route) or `/reports/:rid` (R49 GET
// /api/sys/perf-reports/{rid}). When every chart is drawn the root carries data-report-ready="true". The viewport is
// suspended while the page is open. Texts from the JSON are sanitised.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import type { RouteProps } from '@/app/router/router'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { apiGet, ApiError } from '@/net/api'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { LfBarRank } from '@/ui/lf/LfBarRank'
import { LfChartCard } from '@/ui/lf/LfChartCard'
import { LfStat } from '@/ui/lf/LfStat'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { PanelEmpty } from '@/ui/brand'
import { StatusBadge, type BadgeKind } from '@/ui/notify/StatusBadge'
import { frameCap } from '@/ui/shell/frameCap'
import { ReportCover } from './ReportCover'

type Status = 'PASS' | 'FAIL' | 'ENV_UNMET' | 'WARN' | 'NA' | 'WAIVED'
export interface ReportMetric { key: string; unit: string; runs: (number | null)[]; median: number | null; threshold: { op: string; value: number } | null; status: Status; baseline?: number | null; delta?: number | null }
export interface ReportCase { id: string; ac_ids: string[]; world_id?: string | null; scene?: string | null; priority: 'P0' | 'P1' | 'P2'; layer: 'core' | 'ext'; repeats?: number; status: Status; metrics: ReportMetric[]; errors?: { code: string; message: string }[] }
export interface PerfReport {
  schema: 'awr.perf.report.v1'; run_id: string; gate: string; kind: string; git: { sha: string; branch?: string; dirty?: boolean }
  build: { id?: string; mode: string; contracts: string }; env: Record<string, unknown>; cases: ReportCase[]
  summary: { p0_pass: number; p0_total: number; p1_pass?: number; p1_total?: number; p1_pass_rate_pct?: number; regressions?: string[]; waivers?: string[] }
}

const STATUS_KIND: Readonly<Record<Status, BadgeKind>> = { PASS: 'nominal', FAIL: 'critical-secondary', ENV_UNMET: 'stale', WARN: 'warning', NA: 'muted', WAIVED: 'muted' }
const STATUS_ICON: Readonly<Record<Status, IconKey>> = { PASS: 'check', FAIL: 'alert.critical', ENV_UNMET: 'state.stale', WARN: 'alert.warning', NA: 'minus', WAIVED: 'minus' }
const SEVERITY: Readonly<Record<Status, number>> = { FAIL: 5, ENV_UNMET: 3, WARN: 2, NA: 0, WAIVED: 0, PASS: 0 }

/** a same-origin relative source path (no scheme, no protocol-relative URL) */
export function safeSrc(src: string | null): string | null {
  if (!src || /^[a-z][a-z0-9+.-]*:/i.test(src) || src.startsWith('//')) return null
  return src
}

/** date of a run id pYYYYMMDD-HHMMSS-sha */
export function runDate(runId: string): string {
  const m = /^[pb](\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(runId)
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : '—'
}

const metricOf = (c: ReportCase, key: string): ReportMetric | undefined => c.metrics.find((m) => m.key === key)
const num = (v: number | null | undefined, digits = 1) => fmt.num(v ?? Number.NaN, digits)

/** the four AT A GLANCE figures (pure; exported for tests) */
export function glance(r: PerfReport): { p0: string; szP95: number; ttfpMax: number; simCpu: number } {
  const sz = r.cases.find((c) => c.world_id === 'shenzhen' && c.scene === 'full' && metricOf(c, 'frame_p95_ms'))
  let ttfp = Number.NaN
  let cpu = Number.NaN
  for (const c of r.cases) {
    const tm = metricOf(c, 'ttfp_ms')?.median
    if (typeof tm === 'number' && !(tm <= ttfp)) ttfp = tm
    const cm = metricOf(c, 'sim_cpu_core')?.median
    if (typeof cm === 'number' && !(cm <= cpu)) cpu = cm
  }
  return { p0: `${r.summary.p0_pass} / ${r.summary.p0_total}`, szP95: metricOf(sz ?? { metrics: [] } as unknown as ReportCase, 'frame_p95_ms')?.median ?? Number.NaN, ttfpMax: ttfp, simCpu: cpu }
}

function StatusCell({ s }: { s: Status }) {
  const t = useT()
  return (
    <span className="inline-flex items-center gap-1" data-status-text={s}>
      <StatusBadge kind={STATUS_KIND[s]} text={t(`report.status.${s}`)} />
      {s === 'PASS' || s === 'NA' || s === 'WAIVED' ? <Icon icon={STATUS_ICON[s]} /> : null}
    </span>
  )
}

function ReportBody({ r }: { r: PerfReport }) {
  const t = useT()
  const g = glance(r)
  let hot: ReportCase | null = null
  for (const c of r.cases) if (c.status === 'FAIL' && c.priority === 'P0' && (!hot || SEVERITY[c.status] > SEVERITY[hot.status])) hot = c
  const keyMetric = (c: ReportCase) => c.metrics.find((m) => m.threshold) ?? c.metrics[0]
  const gateCols: LfColumn<ReportCase>[] = [
    { key: 'id', label: t('report.col.case'), format: (c) => <span className="font-mono">{sanitizeText(c.id, 64)}</span> },
    { key: 'priority', label: t('report.col.priority') },
    { key: 'status', label: t('report.col.status'), format: (c) => <StatusCell s={c.status} /> },
    { key: 'metric', label: t('report.col.metric'), format: (c) => <span className="font-mono">{keyMetric(c)?.key ?? '—'}</span> },
    { key: 'median', label: t('report.col.median'), align: 'right', format: (c) => { const m = keyMetric(c); return m ? `${num(m.median, 2)} ${m.unit}` : '—' } },
    { key: 'threshold', label: t('report.col.threshold'), align: 'right', format: (c) => { const m = keyMetric(c); return m?.threshold ? `${m.threshold.op} ${num(m.threshold.value, 2)}` : '—' } },
    { key: 'ac', label: 'AC', format: (c) => <span className="text-muted-foreground">{c.ac_ids.join(' ')}</span> },
  ]
  const frames = r.cases.filter((c) => metricOf(c, 'frame_p95_ms')).map((c) => ({ label: c.id, value: metricOf(c, 'frame_p95_ms')!.median ?? 0 }))
  const worstFrame = frames.reduce<{ label: string; value: number } | null>((a, b) => (!a || b.value > a.value ? b : a), null)
  const ttfp = r.cases.filter((c) => metricOf(c, 'ttfp_ms')).map((c) => ({ label: c.world_id ?? c.id, value: metricOf(c, 'ttfp_ms')!.median ?? 0 }))
  const all = r.cases.flatMap((c) => c.metrics.map((m) => ({ c, m })))
  const baseline = all.filter((x) => typeof x.m.baseline === 'number')
  const env = Object.entries(r.env).filter(([, v]) => v !== null && typeof v !== 'object')
  return (
    <>
      <ReportCover info={{ runId: r.run_id, gate: r.gate, kind: r.kind, date: runDate(r.run_id), commit: `${r.git.sha.slice(0, 12)}${r.git.dirty ? ' *' : ''}` }} />
      <section data-report-section="glance" className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <LfStat density="editorial" label={t('report.glance.p0')} value={Number.NaN} format={() => g.p0} />
        <LfStat density="editorial" label={t('report.glance.szP95')} value={g.szP95} format={(v) => num(v)} unit="ms" />
        <LfStat density="editorial" label={t('report.glance.ttfpMax')} value={g.ttfpMax} format={(v) => num(v, 0)} unit="ms" />
        <LfStat density="editorial" label={t('report.glance.simCpu')} value={g.simCpu} format={(v) => num(v, 2)} unit={t('report.core')} />
      </section>
      <Card data-report-section="gate">
        <CardHeader><CardTitle className="text-ed-title">{t('report.section.gate')}</CardTitle></CardHeader>
        <CardContent>
          <LfTable columns={gateCols} rows={r.cases} rowKey={(c) => c.id} hot={hot ? { row: hot.id, col: 'status' } : null} height={100_000} ariaLabel={t('report.section.gate')} figureId="report-gate" />
        </CardContent>
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <LfChartCard density="editorial" title={t('report.section.frames')} sub={t('report.sub.frames')} src="PERF · FRAME_P95_MS" figureId="report-frames">
          {frames.length ? <LfBarRank variant="rung" data={frames} hero={worstFrame?.label ?? null} height={220} format={(v) => num(v)} ariaLabel={t('report.section.frames')} /> : <p className="text-sm text-muted-foreground">{t('perf.noData')}</p>}
        </LfChartCard>
        <LfChartCard density="editorial" title={t('report.section.cities')} sub={t('report.sub.cities')} src="PERF · TTFP_MS" figureId="report-cities">
          {ttfp.length ? <LfBarRank variant="ticks" data={ttfp} height={220} format={(v) => num(v, 0)} ariaLabel={t('report.section.cities')} /> : <p className="text-sm text-muted-foreground">{t('perf.noData')}</p>}
        </LfChartCard>
      </div>
      <Card data-report-section="baseline">
        <CardHeader><CardTitle className="text-ed-title">{t('report.section.baseline')}</CardTitle></CardHeader>
        <CardContent>
          {baseline.length ? (
            <LfTable columns={[
              { key: 'case', label: t('report.col.case'), format: (x: (typeof baseline)[number]) => <span className="font-mono">{x.c.id}</span> },
              { key: 'key', label: t('report.col.metric'), format: (x) => <span className="font-mono">{x.m.key}</span> },
              { key: 'baseline', label: t('report.col.baseline'), align: 'right', format: (x) => num(x.m.baseline, 2) },
              { key: 'median', label: t('report.col.median'), align: 'right', format: (x) => num(x.m.median, 2) },
              { key: 'delta', label: t('report.col.delta'), align: 'right', format: (x) => num(x.m.delta, 2) },
            ]} rows={baseline} rowKey={(x) => `${x.c.id}:${x.m.key}`} height={100_000} ariaLabel={t('report.section.baseline')} />
          ) : <p className="text-sm text-muted-foreground">{t('report.noBaseline')}</p>}
        </CardContent>
      </Card>
      <Card data-report-section="env">
        <CardHeader><CardTitle className="text-ed-title">{t('report.section.env')}</CardTitle></CardHeader>
        <CardContent>
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-sm">
            {env.map(([k, v]) => [<dt key={`k-${k}`} className="font-mono text-muted-foreground">{k}</dt>, <dd key={`v-${k}`} className="font-mono">{sanitizeText(String(v), 128)}</dd>])}
          </dl>
        </CardContent>
      </Card>
      <Card data-report-section="appendix">
        <CardHeader><CardTitle className="text-ed-title">{t('report.section.appendix')}</CardTitle></CardHeader>
        <CardContent>
          <LfTable columns={[
            { key: 'case', label: t('report.col.case'), format: (x: (typeof all)[number]) => <span className="font-mono">{x.c.id}</span> },
            { key: 'key', label: t('report.col.metric'), format: (x) => <span className="font-mono">{x.m.key}</span> },
            { key: 'unit', label: t('report.col.unit') },
            { key: 'median', label: t('report.col.median'), align: 'right', format: (x) => num(x.m.median, 2) },
            { key: 'runs', label: t('report.col.runs'), align: 'right', format: (x) => x.m.runs.map((v) => num(v, 1)).join(' ') },
            { key: 'status', label: t('report.col.status'), format: (x) => t(`report.status.${x.m.status}`) },
          ]} rows={all} rowKey={(x) => `${x.c.id}:${x.m.key}`} height={100_000} ariaLabel={t('report.section.appendix')} />
        </CardContent>
      </Card>
    </>
  )
}

type Load = { state: 'loading' } | { state: 'ok'; r: PerfReport } | { state: 'missing' } | { state: 'error'; message: string }

export function ReportPage({ params, search }: RouteProps) {
  const t = useT()
  const [load, setLoad] = React.useState<Load>({ state: 'loading' })
  const rootRef = React.useRef<HTMLElement>(null)
  const rid = params.rid ?? null
  const src = safeSrc(search.get('src'))
  // light paper theme and a suspended viewport while the report is open
  React.useEffect(() => {
    const html = document.documentElement
    const wasDark = html.classList.contains('dark')
    html.classList.remove('dark')
    html.style.colorScheme = 'light'
    frameCap.suspend('report', true)
    return () => {
      if (wasDark) html.classList.add('dark')
      html.style.colorScheme = ''
      frameCap.suspend('report', false)
    }
  }, [])
  React.useEffect(() => {
    let alive = true
    const p: Promise<PerfReport> = rid ? apiGet<PerfReport>(`/api/sys/perf-reports/${encodeURIComponent(rid)}`)
      : src ? fetch(src, { headers: { accept: 'application/json' } }).then((r) => {
        if (!r.ok) throw new ApiError(r.status, null, `${r.status}`)
        return r.json() as Promise<PerfReport>
      }) : Promise.reject(new ApiError(404, null, 'no source'))
    p.then((r) => {
      if (!alive) return
      if (r?.schema !== 'awr.perf.report.v1' || !Array.isArray(r.cases)) setLoad({ state: 'error', message: t('report.badSchema') })
      else setLoad({ state: 'ok', r })
    }, (e: unknown) => {
      if (!alive) return
      if (e instanceof ApiError && e.status === 404) setLoad({ state: 'missing' })
      else setLoad({ state: 'error', message: e instanceof Error ? sanitizeText(e.message) : '' })
    })
    return () => {
      alive = false
    }
  }, [rid, src, t])
  // every chart is static SVG: ready after the commit and two animation frames
  React.useEffect(() => {
    if (load.state !== 'ok' && load.state !== 'missing') return
    let a = 0
    let b = 0
    a = requestAnimationFrame(() => {
      b = requestAnimationFrame(() => rootRef.current?.setAttribute('data-report-ready', 'true'))
    })
    return () => {
      cancelAnimationFrame(a)
      cancelAnimationFrame(b)
    }
  }, [load])
  return (
    <main ref={rootRef} data-view="report" data-report-ready="false" className="fixed inset-0 z-(--z-overlay-page) overflow-auto bg-background text-foreground">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 p-8">
        {load.state === 'loading' ? <Skeleton className="h-96 rounded-xl" /> : null}
        {load.state === 'missing' ? (
          <PanelEmpty title={t('report.missing')} action={<Button size="sm" variant="outline" onClick={() => navigate('/worlds')}>{t('world.backToList')}</Button>} />
        ) : null}
        {load.state === 'error' ? <PanelEmpty title={t('report.error')} description={load.message} /> : null}
        {load.state === 'ok' ? <ReportBody r={load.r} /> : null}
      </div>
    </main>
  )
}
