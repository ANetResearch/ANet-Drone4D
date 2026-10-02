// Charts tab (M15-FR-021; AWR-14 §10.1, §10.2): small multiples of the focused vehicle (altitude, speed, battery) as live
// lines. One overlay-phase sampler (10 Hz, 4 Hz on Tier S, registered only while the tab is mounted) pushes the vehicle's
// swarm columns into LfRings (120 s at 10 Hz); drawing goes through the LfScheduler and its Tier S streaming slots
// (cards without a slot show the paused notice). Rings reset when the focus changes. Empty until a vehicle is selected.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { register } from '@/engine'
import { fmt } from '@/lib/format'
import { rtClient } from '@/net/rt'
import { LfChartCard } from '@/ui/lf/LfChartCard'
import { LfLine } from '@/ui/lf/LfLine'
import { LfRing } from '@/ui/lf/series'
import { useElementWidth } from '@/ui/layout/useElementWidth'
import { PanelEmpty } from '@/ui/brand'
import { selectionStore } from '@/stores/selection'
import { useVisibleState } from '@/ui/panels/PanelHost'

const CHARTS = [
  { key: 'alt', unit: 'm', domain: [0, 150] as const },
  { key: 'speed', unit: 'm/s', domain: [0, 15] as const },
  { key: 'battery', unit: '%', domain: [0, 100] as const },
] as const

function ChartCell({ k, unit, domain, ring }: { k: string; unit: string; domain: readonly [number, number]; ring: LfRing }) {
  const t = useT()
  const [ref, w] = useElementWidth<HTMLDivElement>(200)
  const [paused, setPaused] = React.useState(false)
  const onSlot = React.useCallback((has: boolean) => setPaused(!has), [])
  return (
    <LfChartCard title={t(`charts.${k}`)} sub={unit} src={t('charts.src')} paused={paused} figureId={`chart-${k}`}>
      <div ref={ref}>{w > 0 ? <LfLine mode="live" series={ring} domain={domain} width={w} height={64} ariaLabel={t(`charts.${k}`)} format={(v) => fmt.num(v, 1)} onSlot={onSlot} /> : null}</div>
    </LfChartCard>
  )
}

/** sample the focused vehicle into the rings (swarm columns of the page client) */
function sampler(id: string, rings: readonly LfRing[]): (ctx: { nowMs: number }) => void {
  return (ctx) => {
    const rt = rtClient()
    if (!rt) return
    const no = rt.roster.agentNoOf(id)
    const sw = rt.swarm
    let i = -1
    for (let k = 0; k < sw.n; k++) {
      if (sw.agentNo[k] === no) {
        i = k
        break
      }
    }
    if (i < 0) return
    const vel = sw.vel
    rings[0].push(ctx.nowMs, sw.pos[3 * i + 2])
    rings[1].push(ctx.nowMs, Math.hypot(vel[3 * i], vel[3 * i + 1], vel[3 * i + 2]))
    rings[2].push(ctx.nowMs, sw.battery[i] === 255 ? Number.NaN : sw.battery[i])
  }
}

export function ChartsPanel() {
  const t = useT()
  // held while the Dock is folded (ADR-069): a selection change does not re-render the hidden charts
  const primary = useVisibleState(selectionStore).primary
  const rings = React.useMemo(() => CHARTS.map(() => new LfRing()), [])
  React.useEffect(() => {
    for (const r of rings) r.clear()
    if (!primary) return
    const fn = sampler(primary, rings)
    const offS = register('overlay', 'ui.charts.s', fn, { fps: 4, tiers: ['S'] })
    const offBA = register('overlay', 'ui.charts.ba', fn, { fps: 10, tiers: ['A', 'B'] })
    return () => {
      offS()
      offBA()
    }
  }, [primary, rings])
  if (!primary) return <PanelEmpty title={t('charts.noSelection')} />
  return (
    <div className="grid grid-cols-3 gap-2" data-charts={primary}>
      {CHARTS.map((c, i) => <ChartCell key={c.key} k={c.key} unit={c.unit} domain={c.domain} ring={rings[i]} />)}
    </div>
  )
}
