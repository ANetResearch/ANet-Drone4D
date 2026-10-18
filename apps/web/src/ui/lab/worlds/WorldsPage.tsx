// /lab/worlds，世界列表（AWR-14 §21.2.2、§21.3；BN-10；D3-AC-51、UX-AC-083）。LfTable 列：世界 id、名称、角色、内容版本、尺度等级、
// 米制状态（未完成米制标定的行为该表唯一的 hot 单元：事件不能加载，600）、范围、事件数、图层数、许可、QA。行点击进入世界详情。
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getLocale, tOr, useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { benchGet } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import { Button } from '@/ui/components/ui/button'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { useBenchSession } from '@/ui/lab/useBenchSession'
import { LabHeader, useDocTitle } from './LabHeader'
import { extentM, fmtFixed, metricKey, worldName, type WorldItem } from './worlds'

export const WORLDS_QUERY = ['lab', 'worlds'] as const

export function WorldsPage() {
  const t = useT()
  const qc = useQueryClient()
  const session = useBenchSession()
  useDocTitle(t('lab.worlds.docTitle'))
  const q = useQuery({ queryKey: WORLDS_QUERY, queryFn: () => benchGet<{ items: WorldItem[] }>('/worlds'), enabled: session !== null, staleTime: 30_000 })
  const rows = q.data?.items ?? []
  const zh = getLocale() === 'zh-CN'
  const firstBad = rows.find((w) => !w.metric_ok)?.world_id ?? null
  const cols: LfColumn<WorldItem>[] = [
    { key: 'id', label: t('lab.worlds.col.id'), format: (w) => <span className="font-mono">{sanitizeText(w.world_id, 64)}</span> },
    { key: 'name', label: t('lab.worlds.col.name'), format: (w) => sanitizeText(worldName(w, zh), 64) || '—' },
    { key: 'role', label: t('lab.worlds.col.role'), format: (w) => (w.role ? tOr(`lab.worlds.role.${w.role}`, sanitizeText(w.role, 32)) : '—') },
    { key: 'version', label: t('lab.worlds.col.version'), format: (w) => <span className="font-mono">{sanitizeText(w.content_version ?? '—', 16)}</span> },
    { key: 'scale', label: t('lab.worlds.col.scale'), format: (w) => <span className="font-mono">{sanitizeText(w.scale_status ?? '—', 16)}</span> },
    { key: 'metric', label: t('lab.worlds.col.metric'), format: (w) => (
      <span data-world-metric={metricKey(w.metric_ok, w.ranked_ok)} title={w.metric_reason ? sanitizeText(w.metric_reason, 120) : undefined}>
        {t(`lab.worlds.metric.${metricKey(w.metric_ok, w.ranked_ok)}`)}
      </span>
    ) },
    { key: 'extent', label: t('lab.worlds.col.extent'), align: 'right', format: (w) => {
      const e = extentM(w.bounds_m)
      return <span className="font-mono">{e ? t('lab.worlds.extentValue', { x: fmtFixed(e[0], 0), y: fmtFixed(e[1], 0) }) : '—'}</span>
    } },
    { key: 'events', label: t('lab.worlds.col.events'), align: 'right', format: (w) => fmt.count(w.events_count) },
    { key: 'layers', label: t('lab.worlds.col.layers'), align: 'right', format: (w) => fmt.count(w.layers_count) },
    { key: 'license', label: t('lab.worlds.col.license'), format: (w) => <span className="font-mono">{sanitizeText(w.license_id ?? '—', 24)}</span> },
    { key: 'qa', label: t('lab.worlds.col.qa'), format: (w) => <span className="font-mono">{sanitizeText(w.qa_status ?? '—', 16)}</span> },
  ]
  const refresh = () => void qc.invalidateQueries({ queryKey: WORLDS_QUERY })
  return (
    <section data-view="lab-worlds" data-figure="lab-worlds" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <LabHeader page="worlds" icon="nav.world" title={t('lab.worlds.title')} session={session} onRefresh={refresh} />
      {q.isPending ? (
        <div className="flex flex-col gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
      ) : q.isError ? (
        <PanelEmpty title={t('lab.unreachable')} description={t('lab.unreachableHint')}
          action={<Button size="sm" variant="outline" onClick={refresh}>{t('common.retry')}</Button>} />
      ) : !rows.length ? (
        <PanelEmpty title={t('lab.worlds.empty')} description={t('lab.worlds.emptyHint')} />
      ) : (
        <LfTable columns={cols} rows={rows} rowKey={(w) => w.world_id} ariaLabel={t('lab.worlds.title')} figureId="lab-worlds-table"
          hot={firstBad ? { row: firstBad, col: 'metric' } : null}
          onRowClick={(w) => navigate(`/lab/worlds/${encodeURIComponent(w.world_id)}`)}
          height={Math.max(240, typeof window !== 'undefined' ? window.innerHeight - 140 : 480)} />
      )}
    </section>
  )
}
