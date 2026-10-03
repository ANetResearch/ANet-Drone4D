// Performance HUD (M15-FR-093, FR-094; AWR-14 §3.7): a HUD-density LfChartCard anchored to the bottom-left corner of the
// unobscured rect. Row 1: presented interval p95 KPI and the tier badge ("S tier, 30 FPS target"); row 2: sparkline of
// __perf.frame.interval with the T* dashed line; row 3: points / budget with the 20-tick mini gauge (1 tick = 5%); row 4:
// load coverage, requests in flight, limitedBy; row 5: the degradation step, only while degraded. Everything reads
// stores/perf (M06) and the __perf rings; the HUD computes nothing itself. The KPI turns red text only when
// p95 > 1.5 T* (one-red rule). P toggles expanded and single-row; a click opens the Perf tab of the Dock.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { Badge } from '@/ui/components/ui/badge'
import { Progress } from '@/ui/components/ui/progress'
import { Icon } from '@/ui/icons/Icon'
import { LfChartCard } from '@/ui/lf/LfChartCard'
import { LfSparkline } from '@/ui/lf/LfSparkline'
import { LfTickGauge } from '@/ui/lf/LfTickGauge'
import { LfRing, seriesFromPerfRing, type LfSeries, type PerfRing } from '@/ui/lf/series'
import { perfStore, usePerf } from '@/stores/perf'
import { prefs, prefsStore, usePrefs } from '@/stores/prefs'
import { layoutActions } from '@/ui/layout/layoutState'
import { governorText } from './governorToasts'

const EMPTY = new LfRing(2)
/** the __perf frame ring exists once the governor has picked a tier (M06); before that the sparkline is empty */
function frameSeries(tier: string | null): LfSeries {
  if (!tier) return EMPTY
  const p = (globalThis as unknown as { __perf?: { frame?: { interval?: PerfRing } } }).__perf
  const r = p?.frame?.interval
  return r && r.buf ? seriesFromPerfRing(r) : EMPTY
}

/** p95 above 1.5 T* (the KPI's one red); a boolean selector, so the card re-renders when it flips, not with every p95 */
const isHot = (s: { p95Ms: number; targetMs: number }): boolean => Number.isFinite(s.p95Ms) && Number.isFinite(s.targetMs) && s.p95Ms > 1.5 * s.targetMs

/**
 * the rows that follow the 4 Hz summary (points, coverage, requests, limitedBy, degradation step): their own component,
 * so a summary write re-renders these lines and not the chart card, its badges and the sparkline (P4-UI, D1-AC-23)
 */
function HudDetail() {
  const t = useT()
  const B = usePerf((s) => s.B)
  const progress = usePerf((s) => s.progress)
  const inflight = usePerf((s) => s.inflight)
  const limitedBy = usePerf((s) => s.limitedBy)
  const step = usePerf((s) => s.governorStep)
  const stepKey = usePerf((s) => s.governorLabelKey)
  return (
    <>
      <div className="flex items-center justify-between gap-2 text-hud-sub">
        <span>{t('hud.points', { b: fmt.pts(B) })}</span>
        <LfTickGauge mini value={Number.isFinite(progress) ? progress * 100 : 0} ariaLabel={t('hud.coverage')} />
      </div>
      <div className="flex items-center gap-2 text-hud-sub text-muted-foreground">
        <Progress value={Number.isFinite(progress) ? progress * 100 : 0} className="w-16" aria-label={t('hud.coverage')} />
        <span>{fmt.pct(progress * 100)}</span>
        <span>{t('hud.inflight', { n: fmt.count(inflight) })}</span>
        <span className="truncate">{t(`limitedBy.${limitedBy}`)}</span>
      </div>
      {step > 0 ? (
        <div className="flex items-center gap-1.5 text-hud-sub text-muted-foreground">
          <Icon icon="perf.degraded" />
          {governorText(stepKey, step)}
        </div>
      ) : null}
    </>
  )
}

export function PerfHud({ compactBp }: { compactBp: boolean }) {
  const t = useT()
  const open = usePrefs((s) => s.layout.hud.open) && !compactBp
  const tier = usePerf((s) => s.tier)
  const targetMs = usePerf((s) => s.targetMs)
  const hot = usePerf(isHot)
  const forced = usePerf((s) => s.forced)
  const series = React.useMemo(() => frameSeries(tier), [tier])
  const bind = React.useCallback(() => perfStore.getState().p95Ms, [])
  const fps = Number.isFinite(targetMs) && targetMs > 0 ? Math.round(1000 / targetMs) : Number.NaN
  return (
    <div data-anchor="bottom-left" data-figure="perf-hud" data-island="" className="w-(--hud-w)" style={{ '--hud-w': '16.5rem' } as React.CSSProperties}
      onClick={() => layoutActions.openDockTab('perf')}>
      <LfChartCard
        density="hud"
        title={t('hud.title')}
        action={<span className="flex items-center gap-1">
          {/* a test switch that forces the backend tier is labelled wherever the tier is shown (M16-FR-006; INT-1) */}
          {forced ? <Badge variant="outline" data-perf-forced="">{t('perf.forced')}</Badge> : null}
          <Badge variant="outline">{tier ? t('hud.tier', { tier, fps: fmt.num(fps) }) : t('hud.tierUnknown')}</Badge>
        </span>}
      >
        <div className="flex flex-col gap-1.5">
          <div className="flex items-end justify-between gap-2">
            <span className="flex items-baseline gap-1">
              <HudText bind={bind} className={hot ? 'text-lf-hero-text' : undefined} />
              <span className="text-hud-sub font-semibold text-muted-foreground">ms</span>
            </span>
            {open ? <LfSparkline series={series} target={Number.isFinite(targetMs) ? targetMs : undefined} hero={false} ariaLabel={t('hud.sparkline')} /> : null}
          </div>
          {open ? <HudDetail /> : null}
        </div>
      </LfChartCard>
    </div>
  )
}

/** the KPI text is written in the store write itself (the governor phase of the UI tick frame, ADR-066), never a frame
 * later, so the HUD changes in the same frame as the rest of the shell */
function HudText({ bind, className }: { bind: () => number; className?: string }) {
  const ref = React.useRef<HTMLSpanElement>(null)
  React.useEffect(() => {
    let last = ''
    const write = () => {
      const s = fmt.num(bind(), 1)
      if (s !== last && ref.current) {
        ref.current.textContent = s
        last = s
      }
    }
    write()
    return perfStore.subscribe(write)
  }, [bind])
  return <span ref={ref} data-numeric="" className={`text-hud-kpi font-extrabold ${className ?? ''}`} />
}

export const perfHudActions = {
  /** P: expanded or single row (AWR-14 §3.7) */
  toggle: () => prefs.setLayout({ hud: { open: !prefsStore.getState().layout.hud.open } }),
}
