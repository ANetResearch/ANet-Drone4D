// World Hub overlay page, /worlds (M15-FR-023; AWR-14 §5.1): one card per world from GET /api/worlds with LfStat (points,
// nodes, highest building, TTFP), LfBarRank of levelsPoints (one rung = the automatic unit) and the validation badge.
// Ready worlds come first (stable order otherwise): without UrbanScene3D data only the generated demo city synthcity is
// built and the six cities show "not built" with the fetch hint (ADR-077).
// Overlay pages cap the viewport at 5 fps while visible (M15-FR-007).
import { useQuery } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { worldsQuery, type WorldListItem } from '@/app/query/options'
import { fmt } from '@/lib/format'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import { LfBarRank, barUnit } from '@/ui/lf/LfBarRank'
import { LfChartCard } from '@/ui/lf/LfChartCard'
import { LfStat } from '@/ui/lf/LfStat'
import { PanelEmpty } from '@/ui/brand'
import { useFrameCapOverlay } from './useFrameCapOverlay'

function WorldCard({ w }: { w: WorldListItem }) {
  const t = useT()
  const levels = (w.levelsPoints ?? []).map((v, i) => ({ label: `L${i}`, value: v }))
  const unit = barUnit(levels)
  return (
    <LfChartCard
      density="hud"
      title={<span className="font-mono">{w.id}</span>}
      sub={w.nameZh ?? w.name}
      src={t('hub.src', { unit: fmt.pts(unit) })}
      figureId={`hub-${w.id}`}
      action={
        <span className="flex items-center gap-1">
          {w.inUse ? <Badge variant="secondary">{t('hub.inUse')}</Badge> : null}
          <Badge variant="outline" data-world-status={w.status ?? 'ready'}>{t(`hub.status.${w.status ?? 'ready'}`)}</Badge>
        </span>
      }
    >
      <div className="grid grid-cols-2 gap-2">
        <LfStat label={t('hub.stat.points')} value={w.stats?.points} format={fmt.pts} />
        <LfStat label={t('hub.stat.nodes')} value={w.stats?.nodes} format={fmt.count} />
        <LfStat label={t('hub.stat.maxH')} value={w.stats?.maxHeightM} format={(v) => fmt.num(v)} unit="m" />
        <LfStat label={t('hub.stat.ttfp')} value={w.stats?.ttfpMs} format={(v) => fmt.num(v)} unit="ms" />
      </div>
      {levels.length ? <LfBarRank variant="rung" data={levels} unit={unit} height={120} format={fmt.pts} ariaLabel={t('hub.levels')} /> : null}
      {w.status === 'missing' ? <p className="mt-2 text-hud-sub text-muted-foreground" data-world-missing-hint="">{t('hub.missingHint')}</p> : null}
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-1">
          {w.anchorKind === 'synthetic' || w.georeferenced === false ? <Badge variant="outline">{t('world.schematic')}</Badge> : null}
          {w.contentVersion ? <span className="font-mono text-hud-cap text-muted-foreground">{`v ${w.contentVersion.slice(0, 6)}`}</span> : null}
        </span>
        <Button size="sm" disabled={w.status !== undefined && w.status !== 'ready'} data-world-open={w.id} onClick={() => navigate(`/world/${w.id}`)}>{t('hub.open')}</Button>
      </div>
    </LfChartCard>
  )
}

/** ready (or in-use) worlds first, the backend order otherwise */
function hubOrder(list: readonly WorldListItem[]): WorldListItem[] {
  const rank = (w: WorldListItem) => (w.inUse ? 0 : (w.status ?? 'ready') === 'ready' ? 1 : 2)
  return list.map((w, i) => ({ w, i })).sort((a, b) => rank(a.w) - rank(b.w) || a.i - b.i).map((x) => x.w)
}

export function WorldHub() {
  const t = useT()
  const q = useQuery(worldsQuery())
  useFrameCapOverlay('world-hub')
  return (
    <section data-view="world-hub" data-figure="world-hub" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon icon="nav.world" />
        <h1 className="text-ed-title font-bold">{t('hub.title')}</h1>
        <Button size="sm" variant="ghost" className="ml-auto" onClick={() => history.length > 1 ? history.back() : navigate('/')}>
          <Icon icon="close" data-icon="inline-start" />
          {t('common.close')}
        </Button>
      </div>
      {q.isPending ? (
        <div className="grid grid-cols-3 gap-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-56 rounded-xl" />)}</div>
      ) : q.isError || !q.data?.length ? (
        <PanelEmpty title={t('hub.unavailable')} description={t('hub.unavailableHint')}
          action={<Button size="sm" variant="outline" onClick={() => void q.refetch()}>{t('common.retry')}</Button>} />
      ) : (
        <div className="grid grid-cols-3 gap-3 2xl:grid-cols-4" data-world-cards={q.data.length}>{hubOrder(q.data).map((w) => <WorldCard key={w.id} w={w} />)}</div>
      )}
    </section>
  )
}
