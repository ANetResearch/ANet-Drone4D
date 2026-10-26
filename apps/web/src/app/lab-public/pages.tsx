// 公开只读构建的内置页面（AWR-14 §21.14.1：首页即样例目录；§21.29 样例运行的缺省视图）。Owner: M15.
// - 样例目录：静态制品索引中的全部样例运行（事件、世界、运行、策略、机队、观测档、制品数），不显示得分与跨策略比较（D3-AC-21）；
// - 样例运行缺省视图：样例说明（"样例运行，不构成排名"）与可下载的视频、时间窗切片、底图（Tier S 的切片下载）；单文件 > 64 MB
//   的制品不在本站提供（AWR-14 §21.14.2 规则 2）。事件回放、多机私有视角与检视面板由 ./samples/*.route.tsx 以同一 pattern 覆盖。
import * as React from 'react'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyTitle } from '@/ui/components/ui/empty'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { useLabT } from './i18n'
import { LAB_PREFIX, labNavigate, onLabLinkClick, type LabPublicRoute, type LabRouteProps } from './router'
import { findSample, sampleFiles, sampleRuns, type LabStaticRun } from './staticIndex'

/** 字节数的显示：MB（单位在表头，lieflat），一位小数；未知为 "—" */
export function fmtMb(bytes: number | null): string {
  return bytes === null ? '—' : (bytes / (1024 * 1024)).toFixed(1)
}

/** 页面级表格的最大高度：表头与每行按 40 px 估计（单元格内边距使实际行高大于 rowHeight），超过 640 px 时表内滚动 */
export function tableMaxHeight(rows: number): number {
  return Math.min(640, 48 + rows * 40)
}

export function samplePath(runId: string): string {
  return `${LAB_PREFIX}/samples/${encodeURIComponent(runId)}`
}

function Mono({ children }: { children: React.ReactNode }) {
  return <span className="font-mono">{children}</span>
}

export function LabEmpty({ title, description, action }: { title: string; description?: string; action?: React.ReactNode }) {
  return (
    <Empty data-empty="" className="gap-3 border border-border p-6">
      <EmptyHeader>
        <EmptyTitle className="text-hud-title">{title}</EmptyTitle>
        {description ? <EmptyDescription className="text-hud-sub">{description}</EmptyDescription> : null}
      </EmptyHeader>
      {action ? <EmptyContent>{action}</EmptyContent> : null}
    </Empty>
  )
}

/** 事件类型的显示名（静态制品索引不带事件类型；未登记的事件只显示事件 id） */
export const EVENT_TYPES: Readonly<Record<string, string>> = {
  'crop-handoff-001': 'rendezvous_handoff',
  'crop-handoff-002': 'rendezvous_handoff',
  'crop-reid-001': 'reid_occlusion',
  'crop-search-001': 'target_search',
  'crop-vehicle-001': 'vehicle_track',
}

/** 按事件分组的样例运行（输入已按事件、运行 id 排序） */
export function groupByEvent(rows: readonly LabStaticRun[]): { eventId: string; eventVersion: number; worldId: string | null; runs: LabStaticRun[] }[] {
  const groups: { eventId: string; eventVersion: number; worldId: string | null; runs: LabStaticRun[] }[] = []
  for (const r of rows) {
    const g = groups.find((x) => x.eventId === r.eventId)
    if (g) g.runs.push(r)
    else groups.push({ eventId: r.eventId, eventVersion: r.eventVersion, worldId: r.worldId, runs: [r] })
  }
  return groups
}

export function CatalogPage({ index }: LabRouteProps) {
  const t = useLabT()
  const rows = sampleRuns(index)
  const groups = groupByEvent(rows)
  const cols: LfColumn<LabStaticRun>[] = [
    {
      key: 'run', label: t('lab.public.catalog.col.run'),
      format: (r) => (
        <a href={samplePath(r.runId)} onClick={(e) => { e.stopPropagation(); onLabLinkClick(e, samplePath(r.runId)) }}
          className="font-mono underline underline-offset-4 outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring" data-sample-link={r.runId}>
          {r.runId}
        </a>
      ),
    },
    { key: 'policy', label: t('lab.public.catalog.col.policy'), format: (r) => <Mono>{r.policy || '—'}</Mono> },
    { key: 'fleet', label: t('lab.public.catalog.col.fleet'), align: 'right', format: (r) => t('lab.public.catalog.fleet', { n: r.nUav }) },
    { key: 'obs', label: t('lab.public.catalog.col.obs'), format: (r) => <Mono>{r.obsLevel || '—'}</Mono> },
    { key: 'files', label: t('lab.public.catalog.col.files'), align: 'right', format: (r) => t('lab.public.catalog.files', { n: sampleFiles(r).length }) },
    {
      key: 'open', label: '', align: 'right', width: '2.5rem',
      format: () => <Icon icon="chev.right" className="size-4 text-muted-foreground" aria-hidden="true" />,
    },
  ]
  return (
    <section data-view="lab-public-catalog" className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h1 className="text-ed-title font-bold">{t('lab.public.catalog.title')}</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">{t('lab.public.catalog.intro')}</p>
      </div>
      {rows.length === 0 ? (
        <LabEmpty title={t('lab.public.catalog.emptyTitle')} description={t('lab.public.catalog.emptyHint')} />
      ) : (
        <div className="flex flex-col gap-6">
          {groups.map((g) => (
            <div key={g.eventId} className="flex flex-col gap-2" data-catalog-event={g.eventId}>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h2 className="font-mono text-sm font-semibold">{g.eventId}</h2>
                {EVENT_TYPES[g.eventId] ? <Badge variant="outline" className="font-mono">{EVENT_TYPES[g.eventId]}</Badge> : null}
                <span className="text-hud-sub text-muted-foreground">
                  {t('lab.public.catalog.eventMeta', { version: g.eventVersion, world: g.worldId ?? '—', n: g.runs.length })}
                </span>
              </div>
              <div className="overflow-x-auto" data-catalog-rows="">
                <LfTable columns={cols} rows={g.runs} rowKey={(r) => r.runId} ariaLabel={t('lab.public.catalog.aria')} figureId="lab-public-catalog"
                  height={tableMaxHeight(g.runs.length)} rowHeight={44} onRowClick={(r) => labNavigate(samplePath(r.runId))} />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

export function SampleFilesPage({ index, params }: LabRouteProps) {
  const t = useLabT()
  const run = findSample(index, params.sid ?? '')
  const back = (
    <Button variant="ghost" size="sm" nativeButton={false} render={<a href={`${LAB_PREFIX}/`} onClick={(e) => onLabLinkClick(e, `${LAB_PREFIX}/`)} />}>
      <Icon icon="chev.left" data-icon="inline-start" />
      {t('lab.public.sample.back')}
    </Button>
  )
  if (!run) {
    return <LabEmpty title={t('lab.public.sample.unknownTitle')} description={t('lab.public.sample.unknownHint', { id: params.sid ?? '' })} action={back} />
  }
  const files = sampleFiles(run)
  type Row = (typeof files)[number]
  const cols: LfColumn<Row>[] = [
    { key: 'kind', label: t('lab.public.sample.col.kind'), format: (f) => t(`lab.public.sample.kind.${f.kind}`) },
    { key: 'file', label: t('lab.public.sample.col.file'), format: (f) => <Mono>{f.label}</Mono> },
    { key: 'size', label: t('lab.public.sample.col.size'), unit: 'MB', align: 'right', format: (f) => <Mono>{fmtMb(f.bytes)}</Mono> },
    {
      key: 'get', label: '', align: 'right',
      format: (f) => (f.hosted ? (
        <Button variant="ghost" size="sm" nativeButton={false} render={<a href={index.url(f.path)} download="" data-sample-file={f.path} />}>
          <Icon icon="data.export" data-icon="inline-start" />
          {t('lab.public.sample.download')}
        </Button>
      ) : <span className="text-muted-foreground">{t('lab.public.sample.offsite')}</span>),
    },
  ]
  return (
    <section data-view="lab-public-sample" data-sample={run.runId} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        {back}
        <h1 className="text-ed-title font-bold">{t('lab.public.sample.title', { id: run.runId })}</h1>
        <Badge variant="outline">{t('lab.public.sample.notice')}</Badge>
      </div>
      <p className="text-sm text-muted-foreground">
        {t('lab.public.sample.meta', { event: run.eventId, version: run.eventVersion, world: run.worldId ?? '—', policy: run.policy || '—', n: run.nUav, obs: run.obsLevel || '—' })}
      </p>
      <div className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">{t('lab.public.sample.filesTitle')}</h2>
        <p className="text-hud-sub text-muted-foreground">{t('lab.public.sample.filesHint')}</p>
        <div className="overflow-x-auto">
          <LfTable columns={cols} rows={files} rowKey={(f) => f.path} ariaLabel={t('lab.public.sample.filesAria')} figureId="lab-public-sample-files"
            height={tableMaxHeight(files.length)} />
        </div>
      </div>
    </section>
  )
}

/** 内置路由（扩展路由以同一 pattern 覆盖） */
export const BUILTIN_ROUTES: readonly LabPublicRoute[] = [
  { id: 'labPublicHome', pattern: LAB_PREFIX, title: (_p, t) => t('lab.public.catalog.title'), render: (p) => <CatalogPage {...p} /> },
  { id: 'labPublicSamples', pattern: `${LAB_PREFIX}/samples`, title: (_p, t) => t('lab.public.catalog.title'), render: (p) => <CatalogPage {...p} /> },
  {
    id: 'labPublicSample', pattern: `${LAB_PREFIX}/samples/:sid`, title: (p, t) => t('lab.public.sample.title', { id: p.params.sid ?? '' }),
    render: (p) => <SampleFilesPage {...p} />,
  },
]
