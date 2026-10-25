// /lab/worlds/:wid，世界详情（AWR-14 §21.3.2；BN-11 = M22 world_summary；D3-AC-51、UX-AC-083）。六个标签（`?tab=`）：
// 概览（名称、角色、内容版本、范围与面积、高程、地理锚点、语义锚点计数、构建与 QA，以及该世界上的事件）；标定（尺度带单位与
// 条件、配准方法与残差、真北偏航、调平两法倾角与差值；差值 > 0.5° 时红描边 Alert 与"需要控制点复核"；assumed 尺度的说明）；
// 质量（覆盖与缺陷占比、QA 消息）；图层（id、类型、格式、体积）；传感器提示（纹素、边长、纹理受限距离、烘焙太阳方位与说明）；
// 许可与署名（许可、原作者与来源、修改说明、派生来源）。三维场景视图（Tier S 点云）不在本页（P1 起由事件舞台网格提供）。
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getLocale, tOr, useT } from '@/app/i18n'
import { navigate, updateSearch, type RouteProps } from '@/app/router/router'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { BenchError, benchGet } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import { Alert, AlertDescription, AlertTitle } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsPanels, TabsTrigger } from '@/ui/components/ui/tabs'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { useBenchSession } from '@/ui/lab/useBenchSession'
import { LabHeader, useDocTitle } from './LabHeader'
import {
  anchorUncertainty, fmtFixed, LAB_ID_RE, fmtFrac, fmtIso, fmtUnixNs, levelingDelta, levelingWarn, metricKey, parseWorldTab, qaMessage, shortHash,
  summaryExtent, WORLD_TABS, worldName, type EventItem, type WorldDerived, type WorldSummary,
} from './worlds'

export interface WorldDoc { world_id: string; summary: WorldSummary; events: EventItem[] }
export const worldQuery = (wid: string) => ['lab', 'world', wid] as const

/** 定义列表的一行 */
function Row({ k, children, warn }: { k: string; children: React.ReactNode; warn?: boolean }) {
  return (
    <>
      <dt className="text-muted-foreground">{k}</dt>
      <dd className={warn ? 'font-mono text-brand-text' : 'font-mono'} data-warn={warn ? '' : undefined}>{children}</dd>
    </>
  )
}
const Dl = ({ children, figure }: { children: React.ReactNode; figure: string }) => (
  <dl data-figure={figure} className="grid grid-cols-[minmax(10rem,auto)_1fr] gap-x-4 gap-y-1 text-hud-sub">{children}</dl>
)

export function WorldDetailPage({ params, search }: RouteProps) {
  const t = useT()
  const qc = useQueryClient()
  const session = useBenchSession()
  const wid = params.wid ?? ''
  const valid = LAB_ID_RE.test(wid)
  React.useEffect(() => {
    if (!valid) navigate('/lab/worlds', { replace: true })
  }, [valid])
  const q = useQuery({ queryKey: worldQuery(wid), queryFn: () => benchGet<WorldDoc>(`/worlds/${encodeURIComponent(wid)}`), enabled: session !== null && valid,
    staleTime: 30_000, retry: (n, e) => !(e instanceof BenchError && e.status === 404) && n < 2 })
  const zh = getLocale() === 'zh-CN'
  const s = q.data?.summary
  const name = s ? worldName(s, zh) || s.id : wid
  useDocTitle(t('lab.world.docTitle', { name }))
  const tab = parseWorldTab(search.get('tab'))
  const refresh = () => void qc.invalidateQueries({ queryKey: worldQuery(wid) })
  return (
    <section data-view="lab-world" data-figure="lab-world" data-world-id={wid} className="app-layer-overlay-page overflow-auto bg-background p-4">
      <LabHeader page="worlds" icon="nav.world" title={name} session={session} onRefresh={refresh}>
        <Button size="sm" variant="ghost" onClick={() => navigate('/lab/worlds')} data-lab-back="">
          <Icon icon="chev.left" data-icon="inline-start" />
          {t('lab.world.back')}
        </Button>
      </LabHeader>
      {q.isPending ? (
        <div className="flex flex-col gap-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
      ) : q.isError ? (
        q.error instanceof BenchError && q.error.status === 404
          ? <PanelEmpty title={t('lab.world.unavailable')} description={t('lab.world.unavailableHint')} />
          : <PanelEmpty title={t('lab.unreachable')} description={t('lab.unreachableHint')}
              action={<Button size="sm" variant="outline" onClick={refresh}>{t('common.retry')}</Button>} />
      ) : s ? (
        <Tabs value={tab} onValueChange={(v) => updateSearch({ tab: v === 'overview' ? null : String(v) })} className="min-w-0">
          <TabsList variant="line" data-world-tabs={tab}>
            {WORLD_TABS.map((k) => <TabsTrigger key={k} value={k} data-world-tab={k}>{t(`lab.world.tab.${k}`)}</TabsTrigger>)}
          </TabsList>
          <TabsPanels className="min-w-0 pt-3">
            <TabsContent value="overview"><Overview s={s} events={q.data.events} zh={zh} /></TabsContent>
            <TabsContent value="calibration"><Calibration s={s} /></TabsContent>
            <TabsContent value="quality"><Quality s={s} /></TabsContent>
            <TabsContent value="layers"><Layers s={s} /></TabsContent>
            <TabsContent value="sensors"><Sensors s={s} /></TabsContent>
            <TabsContent value="license"><License s={s} /></TabsContent>
          </TabsPanels>
        </Tabs>
      ) : null}
    </section>
  )
}

export function Overview({ s, events, zh }: { s: WorldSummary; events: readonly EventItem[]; zh: boolean }) {
  const t = useT()
  const e = summaryExtent(s)
  const a = s.anchor
  const mk = metricKey(s.metric.ok, s.metric.rankedOk)
  const cols: LfColumn<EventItem>[] = [
    { key: 'id', label: t('lab.events.col.id'), format: (r) => <span className="font-mono">{sanitizeText(r.event_id, 64)}</span> },
    { key: 'name', label: t('lab.events.col.name'), format: (r) => sanitizeText(r.name ?? '—', 64) },
    { key: 'version', label: t('lab.events.col.version'), align: 'right', format: (r) => <span className="font-mono">{`v${r.version}`}</span> },
    { key: 'status', label: t('lab.events.col.status'), format: (r) => tOr(`lab.events.status.${r.status}`, r.status) },
    { key: 'tape', label: t('lab.events.col.tape'), format: (r) => <span className="font-mono" title={r.tape_content_sha256 ?? undefined}>{shortHash(r.tape_content_sha256)}</span> },
    { key: 'updated', label: t('lab.events.col.updated'), format: (r) => <span className="font-mono">{fmtUnixNs(r.updated_unix_ns)}</span> },
  ]
  return (
    <div className="flex flex-col gap-4" data-world-panel="overview">
      <Dl figure="lab-world-overview">
        <Row k={t('lab.world.f.id')}>{sanitizeText(s.id, 64)}</Row>
        <Row k={t('lab.world.f.name')}>{sanitizeText(worldName(s, zh) || '—', 96)}</Row>
        <Row k={t('lab.world.f.role')}>{s.role ? tOr(`lab.worlds.role.${s.role}`, sanitizeText(s.role, 32)) : '—'}</Row>
        <Row k={t('lab.world.f.profile')}>{sanitizeText(s.profile ?? s.build?.profile ?? '—', 24)}</Row>
        <Row k={t('lab.world.f.version')}>{sanitizeText(s.contentVersion ?? '—', 24)}</Row>
        <Row k={t('lab.world.f.metric')} warn={mk === 'none'}>{t(`lab.worlds.metric.${mk}`)}</Row>
        <Row k={t('lab.world.f.extent')}>{e ? t('lab.worlds.extentValue', { x: fmtFixed(e.x, 1), y: fmtFixed(e.y, 1) }) : '—'}</Row>
        <Row k={t('lab.world.f.area')}>{e ? t('lab.world.f.areaValue', { v: fmtFixed((e.x * e.y) / 1e6, 2) }) : '—'}</Row>
        <Row k={t('lab.world.f.height')}>{e ? t('lab.world.f.heightValue', { lo: fmtFixed(e.zmin, 1), hi: fmtFixed(e.zmax, 1) }) : '—'}</Row>
        <Row k={t('lab.world.f.anchor')}>{a && a.latDeg !== null && a.lonDeg !== null ? t('lab.world.f.anchorValue', {
          lat: fmtFixed(a.latDeg, 6), lon: fmtFixed(a.lonDeg, 6), h: fmtFixed(a.hMslM, 1), u: fmtFixed(anchorUncertainty(a.uncertaintyM).h, 1),
        }) : '—'}</Row>
        <Row k={t('lab.world.f.anchors')}>{t('lab.world.f.anchorsValue', { auto: s.anchorsCount.auto, review: s.anchorsCount.review, confirmed: s.anchorsCount.confirmed })}</Row>
        <Row k={t('lab.world.f.qa')}>{sanitizeText(s.qa.status ?? '—', 16)}</Row>
        {s.build ? <Row k={t('lab.world.f.build')}>{t('lab.world.f.buildValue', { at: fmtIso(s.build.createdAt), v: sanitizeText(s.build.generatorVersion ?? '—', 24) })}</Row> : null}
      </Dl>
      <div>
        <h2 className="mb-2 text-hud-title font-semibold">{t('lab.world.events')}</h2>
        {events.length ? (
          <LfTable columns={cols} rows={events} rowKey={(r) => r.event_id} ariaLabel={t('lab.world.events')} figureId="lab-world-events"
            onRowClick={(r) => navigate(`/lab/events?world=${encodeURIComponent(s.id)}&eid=${encodeURIComponent(r.event_id)}`)} />
        ) : <p className="text-hud-sub text-muted-foreground">{t('lab.world.eventsEmpty')}</p>}
      </div>
    </div>
  )
}

export function Calibration({ s }: { s: WorldSummary }) {
  const t = useT()
  const r = s.registration
  const lev = r?.leveling
  const delta = levelingDelta(lev)
  const warn = levelingWarn(lev)
  const deg = (v: number | null | undefined, d = 3) => (typeof v === 'number' ? `${fmtFixed(v, d)}°` : t('lab.world.cal.notRecorded'))
  const method = r?.method ? tOr(`lab.world.method.${r.method}`, sanitizeText(r.method, 40)) : t('lab.world.cal.notRecorded')
  return (
    <div className="flex flex-col gap-3" data-world-panel="calibration">
      {warn ? (
        <Alert className="border-destructive" data-world-leveling-warn="">
          <Icon icon="alert.warning" />
          <AlertTitle>{t('lab.world.cal.delta')}</AlertTitle>
          <AlertDescription>{t('lab.world.cal.deltaWarn', { d: fmtFixed(delta, 2) })}</AlertDescription>
        </Alert>
      ) : null}
      {s.scaleStatus === 'assumed' ? <p className="text-hud-sub" data-world-scale-assumed="">{t('lab.world.cal.assumed')}</p> : null}
      {!r ? <p className="text-hud-sub text-muted-foreground">{t('lab.world.cal.none')}</p> : (
        <Dl figure="lab-world-calibration">
          <Row k={t('lab.world.cal.scale')}>{typeof r.unitsToMeters === 'number' ? t('lab.world.cal.scaleValue', {
            s: fmtFixed(r.unitsToMeters, 3), method, rmse: typeof r.rmseM === 'number' ? `${fmtFixed(r.rmseM, 2)} m` : t('lab.world.cal.notRecorded'),
          }) : t('lab.world.cal.notRecorded')}</Row>
          <Row k={t('lab.world.cal.scaleStatus')}>{sanitizeText(s.scaleStatus ?? '—', 16)}</Row>
          <Row k={t('lab.world.cal.method')}>{method}</Row>
          <Row k={t('lab.world.cal.trueNorth')}>{s.trueNorth && typeof s.trueNorth.yawOffsetDeg === 'number'
            ? t('lab.world.cal.trueNorthValue', { deg: fmtFixed(s.trueNorth.yawOffsetDeg, 2), c: sanitizeText(s.trueNorth.confidence ?? '—', 24) })
            : t('lab.world.cal.notRecorded')}</Row>
          <Row k={t('lab.world.cal.yaw')}>{deg(r.yawDeg, 2)}</Row>
          <Row k={t('lab.world.cal.leveled')}>{deg(r.leveledDeg)}</Row>
          <Row k={t('lab.world.cal.primary')}>{sanitizeText(lev?.primary ?? t('lab.world.cal.notRecorded'), 32)}</Row>
          <Row k={t('lab.world.cal.dem')}>{deg(lev?.demDeg)}</Row>
          <Row k={t('lab.world.cal.wall')}>{deg(lev?.wallDeg)}</Row>
          <Row k={t('lab.world.cal.delta')} warn={warn}>{delta === null ? t('lab.world.cal.notRecorded') : `${fmtFixed(delta, 3)}°`}</Row>
          <Row k={t('lab.world.cal.planes')}>{deg(lev?.planesMaxSlopeDeg, 2)}</Row>
          {s.metric.reason ? <Row k={t('lab.world.cal.metricReason')} warn>{sanitizeText(s.metric.reason, 120)}</Row> : null}
          {s.metric.missing?.length ? <Row k={t('lab.world.cal.missing')}>{s.metric.missing.map((m) => sanitizeText(m, 40)).join(', ')}</Row> : null}
          {s.trueNorth?.evidence?.length ? <Row k={t('lab.world.cal.evidence')}>{s.trueNorth.evidence.map((x) => sanitizeText(x, 120)).join('; ')}</Row> : null}
        </Dl>
      )}
    </div>
  )
}

export function Quality({ s }: { s: WorldSummary }) {
  const t = useT()
  const q = s.quality
  const msgs = s.qa.messages.map(qaMessage).filter(Boolean)
  return (
    <div className="flex flex-col gap-3" data-world-panel="quality">
      {!q ? <p className="text-hud-sub text-muted-foreground">{t('lab.world.q.none')}</p> : (
        <Dl figure="lab-world-quality">
          <Row k={t('lab.world.q.coverage')}>{fmtFrac(q.coverageFrac)}</Row>
          <Row k={t('lab.world.q.nodata')}>{fmtFrac(q.nodataFrac)}</Row>
          <Row k={t('lab.world.q.hole')}>{fmtFrac(q.holeFrac)}</Row>
          <Row k={t('lab.world.q.stretched')}>{fmtFrac(q.stretchedFrac)}</Row>
          <Row k={t('lab.world.q.multilayer')}>{fmtFrac(q.multilayerFrac)}</Row>
        </Dl>
      )}
      <div>
        <h2 className="mb-1 text-hud-title font-semibold">{t('lab.world.q.messages')}</h2>
        {msgs.length ? <ul className="list-disc pl-5 text-hud-sub">{msgs.map((m, i) => <li key={i}>{sanitizeText(m, 200)}</li>)}</ul>
          : <p className="text-hud-sub text-muted-foreground">{t('lab.world.q.noMessages')}</p>}
      </div>
    </div>
  )
}

export function Layers({ s }: { s: WorldSummary }) {
  const t = useT()
  type L = WorldSummary['layers'][number]
  const cols: LfColumn<L>[] = [
    { key: 'id', label: t('lab.world.l.id'), format: (l) => <span className="font-mono">{sanitizeText(l.id, 64)}</span> },
    { key: 'type', label: t('lab.world.l.type'), format: (l) => <span className="font-mono">{sanitizeText(l.type, 32)}</span> },
    { key: 'format', label: t('lab.world.l.format'), format: (l) => <span className="font-mono">{sanitizeText(l.format, 48)}</span> },
    { key: 'bytes', label: t('lab.world.l.bytes'), align: 'right', format: (l) => <span className="font-mono">{l.bytes === null ? '—' : fmt.bytes(l.bytes)}</span> },
  ]
  return (
    <div data-world-panel="layers">
      {s.layers.length ? <LfTable columns={cols} rows={s.layers} rowKey={(l) => l.id} ariaLabel={t('lab.world.tab.layers')} figureId="lab-world-layers" height={480} />
        : <p className="text-hud-sub text-muted-foreground">{t('lab.world.l.empty')}</p>}
    </div>
  )
}

export function Sensors({ s }: { s: WorldSummary }) {
  const t = useT()
  const h = s.sensorHints
  const sun = h?.bakedSunAzElDeg
  return (
    <div className="flex flex-col gap-3" data-world-panel="sensors">
      {!h ? <p className="text-hud-sub text-muted-foreground">{t('lab.world.s.none')}</p> : (
        <>
          <Dl figure="lab-world-sensors">
            <Row k={t('lab.world.s.texel')}>{typeof h.texelM === 'number' ? `${fmtFixed(h.texelM, 4)} m` : '—'}</Row>
            <Row k={t('lab.world.s.edge')}>{typeof h.meshEdgeP50M === 'number' ? `${fmtFixed(h.meshEdgeP50M, 3)} m` : '—'}</Row>
            {Object.entries(h.textureLimitedRangeM ?? {}).map(([k, v]) => (
              <Row key={k} k={t('lab.world.s.range', { profile: sanitizeText(k, 24) })}>{`${fmtFixed(v, 1)} m`}</Row>
            ))}
            <Row k={t('lab.world.s.sun')}>{Array.isArray(sun) && sun.length === 2 ? t('lab.world.s.sunValue', { az: fmtFixed(sun[0], 1), el: fmtFixed(sun[1], 1) })
              : t('lab.world.s.sunUnknown', { c: sanitizeText(h.bakedSunConfidence ?? '—', 24) })}</Row>
          </Dl>
          <p className="text-hud-sub text-muted-foreground" data-world-sensor-note="">{t('lab.world.s.note')}</p>
        </>
      )}
    </div>
  )
}

export function License({ s }: { s: WorldSummary }) {
  const t = useT()
  const L = s.license
  const cols: LfColumn<WorldDerived>[] = [
    { key: 'name', label: t('lab.world.lic.name'), format: (d) => sanitizeText([d.name, d.version].filter(Boolean).join(' ') || '—', 80) },
    { key: 'license', label: t('lab.world.lic.dataset'), format: (d) => <span className="font-mono">{sanitizeText(d.license ?? '—', 24)}</span> },
    { key: 'use', label: t('lab.world.lic.use'), format: (d) => sanitizeText(d.use ?? '—', 80) },
    { key: 'attribution', label: t('lab.world.lic.attribution'), format: (d) => sanitizeText(d.attribution ?? '—', 120) },
  ]
  return (
    <div className="flex flex-col gap-3" data-world-panel="license">
      <Dl figure="lab-world-license">
        <Row k={t('lab.world.lic.dataset')}>{L.dataset ? <Badge variant="outline" className="font-mono">{sanitizeText(L.dataset, 24)}</Badge> : '—'}</Row>
        <Row k={t('lab.world.lic.citation')}>{sanitizeText(L.citation ?? '—', 400)}</Row>
        <Row k={t('lab.world.lic.notice')}>{sanitizeText(L.notice ?? '—', 800)}</Row>
      </Dl>
      <div>
        <h2 className="mb-2 text-hud-title font-semibold">{t('lab.world.lic.derived')}</h2>
        {L.derivedFrom.length ? (
          <LfTable columns={cols} rows={L.derivedFrom} rowKey={(d, i) => `${d.name ?? ''}#${i}`} ariaLabel={t('lab.world.lic.derived')} figureId="lab-world-derived" />
        ) : <p className="text-hud-sub text-muted-foreground">{t('lab.world.lic.derivedNone')}</p>}
      </div>
    </div>
  )
}
