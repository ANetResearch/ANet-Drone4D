// Perf tab (M15-FR-095; AWR-14 §5.5; AWR-18 §9): the 11 cards. Presented interval (live line of __perf.frame.interval,
// T* and 1.5 T* guides, p50 / p95), frame interval distribution (F14 over the last 2048 frames, 1 Hz, bins 0, 8.3, 16.7,
// 33.3, 50, 100 ms and more, the median bin as the hero), point budget (F11 gauge of drawn points against the rung band
// with lo, hi and B_floor), points per level (the world's level counts, F1 rungs), streaming and residency (in flight,
// resident, CPU cache, queued), layer budgets (table of __perf.layers, the worst layer is the one hot cell), degradation
// ladder (7 governor steps, the current one as the hero), controller state (rung, limitedBy, frozen frames, rung
// changes), network (RTT, swarm rate, credit skips), latency (focus t_sim to pixel p95, command to visible p95) and
// server (sim step p99, RTF, api CPU from perf/server). Numbers are C-class bound text (<= 4 Hz on Tier S), SVG cards
// refresh <= 1 Hz only while visible, streaming lines go through the LfScheduler slots. "Copy diagnostics" writes the
// __perf.snapshot() summary to the clipboard; dev builds flag forced settings.
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { useRoute } from '@/app/router/router'
import { worldsQuery } from '@/app/query/options'
import { LADDER, perfProbe } from '@/engine'
import { fmt } from '@/lib/format'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { StateIcon } from '@/ui/icons/StateIcon'
import { LfBarRank, barUnit } from '@/ui/lf/LfBarRank'
import { LfChartCard } from '@/ui/lf/LfChartCard'
import { binCounts, LfHistogram, medianBin } from '@/ui/lf/LfHistogram'
import { LfLine } from '@/ui/lf/LfLine'
import { LfStat } from '@/ui/lf/LfStat'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { LfTickGauge } from '@/ui/lf/LfTickGauge'
import { ringQuantile, seriesFromPerfRing, type LfSeries, type PerfRing } from '@/ui/lf/series'
import { useElementWidth } from '@/ui/layout/useElementWidth'
import { serverPerf } from '@/ui/hud/serverPerf'
import { governorText } from '@/ui/hud/governorToasts'
import { PERF_UI } from '@/ui/shell/perfUi'
import { usePerf } from '@/stores/perf'
import { useWorld } from '@/stores/world'

const EDGES = [0, 8.3, 16.7, 33.3, 50, 100, Number.POSITIVE_INFINITY]
const HIST_FRAMES = 2048
const ONE_HZ_MS = 1000
const GOVERNOR_STEPS = 7
const scratch = new Float64Array(HIST_FRAMES)

/** re-render every `ms` while the element is on screen (SVG cards <= 1 Hz; hidden cards never redraw) */
function useVisibleTick(ref: React.RefObject<HTMLElement | null>, ms: number): number {
  const [n, setN] = React.useState(0)
  React.useEffect(() => {
    const el = ref.current
    if (!el) return
    let visible = false
    const io = new IntersectionObserver((es) => {
      visible = es.some((e) => e.isIntersecting)
    })
    io.observe(el)
    const h = setInterval(() => {
      if (visible && !document.hidden) setN((x) => x + 1)
    }, ms)
    return () => {
      io.disconnect()
      clearInterval(h)
    }
  }, [ref, ms])
  return n
}

const P = () => perfProbe()
const readers = {
  rtt: () => P().net.rttMs,
  swarmHz: () => P().net.swarmHz,
  skips: () => serverPerf.mine.creditSkips || P().net.creditSkips,
  stepP99: () => serverPerf.sim.stepP99Us / 1000,
  rtf: () => serverPerf.sim.rtf,
  apiCpu: () => serverPerf.api.cpuPct,
  frozen: () => P().cas.frozenFrames,
  rungChanges: () => P().cas.rungChanges,
}
const lat = new Float64Array(512)
const cmdP95 = () => ringQuantile(P().latency.cmdToVisibleMs as PerfRing, 512, 0.95, lat)
const pixP95 = () => ringQuantile(P().latency.tSimToPixelMs as PerfRing, 512, 0.95, lat)
const ms1 = (v: number) => fmt.num(v, 1)
const ms0 = (v: number) => fmt.num(v, 0)
const hz = (v: number) => fmt.num(v, 1)
const x2 = (v: number) => fmt.num(v, 2)

function frameSeries(): LfSeries | null {
  const p = P()
  const r = p?.frame?.interval as PerfRing | undefined
  return r?.buf ? seriesFromPerfRing(r, p.frame.t as PerfRing) : null
}

function IntervalCard({ targetMs }: { targetMs: number }) {
  const t = useT()
  const [ref, w] = useElementWidth<HTMLDivElement>(240)
  const [paused, setPaused] = React.useState(false)
  const series = React.useMemo(() => frameSeries(), [])
  const p50 = usePerf((s) => s.p50Ms)
  const p95 = usePerf((s) => s.p95Ms)
  const T = Number.isFinite(targetMs) ? targetMs : 33.3
  return (
    <LfChartCard title={t('perf.card.interval')} sub={t('perf.sub.interval', { t: fmt.num(T, 1) })} src="FRAME · __PERF" figureId="perf-interval" paused={paused}>
      <div className="grid grid-cols-2 gap-2">
        <LfStat label="P50" value={p50} format={ms1} unit="ms" />
        <LfStat label="P95" value={p95} format={ms1} unit="ms" hero={Number.isFinite(p95) && p95 > 1.5 * T} />
      </div>
      <div ref={ref}>
        {series && w > 0 ? (
          <LfLine mode="live" series={series} domain={[0, 3 * T]} target={T} guides={[1.5 * T]} width={w} height={64} windowSec={30} hz={4} prio={1}
            ariaLabel={t('perf.card.interval')} format={ms1} onSlot={(has) => setPaused(!has)} />
        ) : null}
      </div>
    </LfChartCard>
  )
}

function DistributionCard() {
  const t = useT()
  const ref = React.useRef<HTMLDivElement>(null)
  const tick = useVisibleTick(ref, ONE_HZ_MS)
  const counts = React.useMemo(() => {
    const r = P().frame.interval as PerfRing
    const m = Math.min(HIST_FRAMES, r.n, r.buf.length)
    const mask = r.buf.length - 1
    for (let i = 0; i < m; i++) scratch[i] = r.buf[(r.n - m + i) & mask]
    return binCounts(scratch, m, EDGES)
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- `tick` is the 1 Hz refresh key of the mutable ring
  }, [tick])
  const med = medianBin(counts)
  return (
    <div ref={ref}>
      <LfChartCard title={t('perf.card.distribution')} sub={t('perf.sub.distribution', { n: fmt.count(counts.reduce((a, b) => a + b, 0)) })} src="F14 · FRAME INTERVAL · MS" figureId="perf-dist">
        <LfHistogram counts={counts} edges={EDGES} hero={med} height={140} format={fmt.count} ariaLabel={t('perf.card.distribution')} />
      </LfChartCard>
    </div>
  )
}

function BudgetCard() {
  const t = useT()
  const B = useWorld((s) => s.B)
  const lo = useWorld((s) => s.lo)
  const hi = useWorld((s) => s.hi)
  const floor = useWorld((s) => s.Bfloor)
  const rung = useWorld((s) => s.rung)
  const band = hi > 0 ? hi : (LADDER[rung.index]?.hi ?? 0)
  return (
    <LfChartCard title={t('perf.card.budget')} sub={t('perf.sub.budget', { rung: LADDER[rung.index]?.name ?? rung.name })} src="F11 · CAS · STORES/WORLD" figureId="perf-budget">
      <LfTickGauge value={band > 0 ? (B / band) * 100 : 0} center={fmt.pts(B)} remainder={t('perf.budgetBand', { lo: fmt.pts(lo), hi: fmt.pts(band), floor: fmt.pts(floor) })} ariaLabel={t('perf.card.budget')} />
    </LfChartCard>
  )
}

function LevelsCard() {
  const t = useT()
  const route = useRoute()
  const worlds = useQuery(worldsQuery())
  const selected = useWorld((st) => st.levelCounts)
  const id = route?.params.id
  const fromWorld = worlds.data?.find((w) => w.id === id)?.levelsPoints ?? []
  const src = selected.length ? selected : fromWorld
  const levels = src.map((v, i) => ({ label: `L${i}`, value: v }))
  const unit = barUnit(levels)
  return (
    <LfChartCard title={t('perf.card.levels')} sub={selected.length ? t('perf.sub.levels') : t('perf.sub.levelsWorld')} src={t('hub.src', { unit: fmt.pts(unit) })} figureId="perf-levels">
      {levels.length ? <LfBarRank variant="rung" data={levels} unit={unit} height={140} format={fmt.pts} ariaLabel={t('perf.card.levels')} /> : <p className="text-hud-sub text-muted-foreground">{t('perf.noData')}</p>}
    </LfChartCard>
  )
}

function StreamingCard() {
  const t = useT()
  const inflight = useWorld((st) => st.inflight)
  const queued = useWorld((st) => st.queued)
  const resident = useWorld((st) => st.residentPts)
  const cache = useWorld((st) => st.cpuCacheBytes)
  return (
    <LfChartCard title={t('perf.card.streaming')} src="PC · STORES/WORLD" figureId="perf-stream">
      <div className="grid grid-cols-2 gap-2">
        <LfStat label={t('perf.inflight')} value={inflight} format={fmt.count} />
        <LfStat label={t('perf.queued')} value={queued} format={fmt.count} />
        <LfStat label={t('perf.resident')} value={resident} format={fmt.pts} />
        <LfStat label={t('perf.cache')} value={cache} format={fmt.bytes} />
      </div>
    </LfChartCard>
  )
}

interface LayerRow { key: string; ms: number; draws: number; verts: number; over: number }
function LayersCard() {
  const t = useT()
  const ref = React.useRef<HTMLDivElement>(null)
  const tick = useVisibleTick(ref, ONE_HZ_MS)
  const rows = React.useMemo<LayerRow[]>(() => {
    const L = P().layers as Record<string, { cpuMs: PerfRing; draws: number; verts: number; overBudgetFrames: number }>
    return Object.entries(L).map(([key, l]) => {
      const r = l.cpuMs
      const last = r.n > 0 ? r.buf[(r.n - 1) & (r.buf.length - 1)] : Number.NaN
      return { key, ms: last, draws: l.draws, verts: l.verts, over: l.overBudgetFrames }
    })
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- `tick` is the 1 Hz refresh key of the mutable probe
  }, [tick])
  let worst: LayerRow | null = null
  for (const r of rows) if (r.over > 0 && (!worst || r.over > worst.over)) worst = r
  const columns: LfColumn<LayerRow>[] = [
    { key: 'key', label: t('perf.col.layer') },
    { key: 'ms', label: 'CPU', unit: 'ms', align: 'right', format: (r) => fmt.num(r.ms, 2) },
    { key: 'draws', label: t('perf.col.draws'), align: 'right', format: (r) => fmt.count(r.draws) },
    { key: 'verts', label: t('perf.col.verts'), align: 'right', format: (r) => fmt.pts(r.verts) },
    { key: 'over', label: t('perf.col.over'), align: 'right', format: (r) => fmt.count(r.over) },
  ]
  return (
    <div ref={ref}>
      <LfChartCard title={t('perf.card.layers')} src="LAYERS · __PERF" figureId="perf-layers">
        <LfTable columns={columns} rows={rows} rowKey={(r) => r.key} hot={worst ? { row: worst.key, col: 'over' } : null} height={150} rowHeight={22} ariaLabel={t('perf.card.layers')} />
      </LfChartCard>
    </div>
  )
}

function LadderCard() {
  const t = useT()
  const step = usePerf((s) => s.governorStep)
  const labelKey = usePerf((s) => s.governorLabelKey)
  const data = Array.from({ length: GOVERNOR_STEPS }, (_, i) => ({ label: String(i + 1), value: i + 1 <= step ? 2 : 1 }))
  return (
    <LfChartCard title={t('perf.card.ladder')} sub={step > 0 ? governorText(labelKey, step) : t('perf.noDegrade')} src="GOVERNOR · 7 STEPS" figureId="perf-ladder">
      <LfBarRank variant="ticks" data={data} unit={1} hero={step > 0 ? String(step) : null} height={120} ariaLabel={t('perf.card.ladder')} />
    </LfChartCard>
  )
}

function ControllerCard() {
  const t = useT()
  const rung = useWorld((s) => s.rung)
  const limitedBy = useWorld((s) => s.limitedBy)
  const backend = usePerf((s) => s.backendState)
  return (
    <LfChartCard title={t('perf.card.controller')} src="CAS · STATE" figureId="perf-cas">
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-hud-sub">
        <dt className="text-muted-foreground">{t('perf.rung')}</dt><dd className="font-mono">{LADDER[rung.index]?.name ?? rung.name}{rung.manual ? ` · ${t('perf.manual')}` : ''}</dd>
        <dt className="text-muted-foreground">{t('perf.limitedBy')}</dt><dd>{t(`limitedBy.${limitedBy}`)}</dd>
        <dt className="text-muted-foreground">{t('perf.backend')}</dt><dd className="font-mono">{backend}</dd>
      </dl>
      <div className="grid grid-cols-2 gap-2">
        <LfStat label={t('perf.frozen')} bind={readers.frozen} format={fmt.count} />
        <LfStat label={t('perf.rungChanges')} bind={readers.rungChanges} format={fmt.count} />
      </div>
    </LfChartCard>
  )
}

function NetworkCard() {
  const t = useT()
  return (
    <LfChartCard title={t('perf.card.network')} src="NET · RT.WORKER" figureId="perf-net">
      <div className="grid grid-cols-3 gap-2">
        <LfStat label="RTT" bind={readers.rtt} format={ms0} unit="ms" />
        <LfStat label="SWARM" bind={readers.swarmHz} format={hz} unit="Hz" />
        <LfStat label="SKIPS" bind={readers.skips} format={fmt.count} />
      </div>
    </LfChartCard>
  )
}

function LatencyCard() {
  const t = useT()
  return (
    <LfChartCard title={t('perf.card.latency')} src="LATENCY · P95" figureId="perf-latency">
      <div className="grid grid-cols-2 gap-2">
        <LfStat label={t('perf.pixP95')} bind={pixP95} format={ms0} unit="ms" />
        <LfStat label={t('perf.cmdP95')} bind={cmdP95} format={ms0} unit="ms" />
      </div>
    </LfChartCard>
  )
}

function ServerCard() {
  const t = useT()
  return (
    <LfChartCard title={t('perf.card.server')} src="PERF/SERVER · 1 HZ" figureId="perf-server">
      <div className="grid grid-cols-3 gap-2">
        <LfStat label={t('perf.stepP99')} bind={readers.stepP99} format={ms1} unit="ms" spark={serverPerf.stepP99} />
        <LfStat label="RTF" bind={readers.rtf} format={x2} />
        <LfStat label="API CPU" bind={readers.apiCpu} format={ms0} unit="%" />
      </div>
    </LfChartCard>
  )
}

export function PerfPanel() {
  const t = useT()
  const targetMs = usePerf((s) => s.targetMs)
  const forced = usePerf((s) => s.forced)
  const [copied, setCopied] = React.useState(false)
  const copy = () => {
    const p = (globalThis as unknown as { __perf?: { snapshot?: () => unknown } }).__perf
    const snap = p?.snapshot ? p.snapshot() : { ui: PERF_UI }
    void navigator.clipboard?.writeText(JSON.stringify(snap)).then(() => setCopied(true), () => undefined)
  }
  React.useEffect(() => {
    if (!copied) return
    const h = setTimeout(() => setCopied(false), ONE_HZ_MS * 2)
    return () => clearTimeout(h)
  }, [copied])
  return (
    <div className="flex flex-col gap-2" data-perf-panel="">
      <div className="flex items-center justify-end gap-2">
        {forced ? <Badge variant="outline" data-perf-forced="">{t('perf.forced')}</Badge> : null}
        <Button size="sm" variant="outline" onClick={copy} data-copy-diagnostics="">
          <StateIcon icon="copy" alt={copied} spring="snappy" data-icon="inline-start" />
          {t('perf.copy')}
        </Button>
      </div>
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-3 2xl:grid-cols-4">
        <IntervalCard targetMs={targetMs} />
        <DistributionCard />
        <BudgetCard />
        <LevelsCard />
        <StreamingCard />
        <LayersCard />
        <LadderCard />
        <ControllerCard />
        <NetworkCard />
        <LatencyCard />
        <ServerCard />
      </div>
    </div>
  )
}
