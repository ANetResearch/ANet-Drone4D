// /lab/runs/:rid/lidar，LiDAR 与重建对比（D3-AC-15"'LiDAR 与重建对比'视图在 Tier B 以裁剪网格正确显示三方叠加，在 Tier S 以点云降级显示；
// 到网格距离着色与 eval_sim 数值一致，RPE、ATE、漂移分布正确"；AWR-05 §7.5；AWR-14 §21.8；UX-AC-095）。内部 D3 构建，最低角色研究者，只读。
// - 专用画布（CompareCanvas）：GLB 真值、L0 已知位姿地图与 FAST-LIVO2 地图三方叠加，可单独开关；Tier B/A 真值为裁剪碰撞网格，Tier S 为网格
//   采样彩色点云；显示档按设备类别（软件光栅为 Tier S），`?tier=` 可覆盖；点与面预算按 compare.ts 核对，超出时确定性抽稀（点）或不显示（网格）；
// - 误差着色只用随点下发的 eval_sim u8（页面不计算到网格距离）；文档自带抽样点逐点核对（≤ 2 mm），结果在图例旁显示并写入 data 属性；
// - 指标面板：五项主指标（跨"种子 × 重复"中位数）、LIVO 与 LIO 两组 RPE100 箱线图（D3-AC-14 区间参考带）、多机覆盖与两两 Jaccard 矩阵（离线 L0）、
//   eval_sim 的到网格距离分布；底部为所选重复的沿程曲线，点击段在画布中定位；
// - URL：`uav`、`src`（gt,l0,livo 子集）、`rep`、`mode`、`tier`、`color`、`size`（replaceState）。
import { useQueries, useQuery, type UseQueryResult } from '@tanstack/react-query'
import * as React from 'react'
import type { Object3D } from 'three'
import { useT } from '@/app/i18n'
import { navigate, updateSearch, type RouteProps } from '@/app/router/router'
import { sanitizeText } from '@/lib/sanitize'
import { BenchError, benchBytes, benchGet, ROLE_RANK } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Toggle } from '@/ui/components/ui/toggle'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { useBenchSession } from '@/ui/lab/useBenchSession'
import { useDocTitle } from '@/ui/lab/worlds/LabHeader'
import { useWindowSize } from '@/ui/layout/layoutState'
import { useElementWidth } from '@/ui/layout/useElementWidth'
import { LfChartCard } from '@/ui/lf/LfChartCard'
import { LfStat } from '@/ui/lf/LfStat'
import { CompareCanvas, probeCompareTier, type CanvasPick } from './CompareCanvas'
import { ErrLegend, LfBox, LfMatrix, SegmentCurve } from './charts'
import {
  boxStats, budgetStrides, checkErrSamples, COLOR_BY, COMPARE_DOC, compareArtifactPath, CompareDocError, decodePointLayer, errU8ToM, layersFor, medianOf,
  meshWithinBudget, metricValues, MODES, parseCompareDoc, parseCompareSearch, POINT_SIZES, resolveSelection, shownCount, SOURCES, TIER_B_MESH_FACES,
  TIER_B_POINTS_PER_MAP, TIER_S_POINTS_TOTAL, type CompareDoc, type CompareTier, type DecodedPoints, type ErrStats, type LayerDesc,
  type Vec3,
} from './compare'
import { parseMeshGlb } from './scene'

export const compareDocKey = (rid: string) => ['lab', 'lidar-compare', rid, 'doc'] as const
export const compareLayerKey = (rid: string, path: string) => ['lab', 'lidar-compare', rid, 'layer', path] as const
interface Interaction { key: string; pick: CanvasPick | null; rect: (ErrStats & { points: number }) | null; seg: number | null }
export type LoadedLayer = { kind: 'points'; data: DecodedPoints } | { kind: 'mesh'; root: Object3D; faces: number }

async function loadLayer(rid: string, l: LayerDesc, signal: AbortSignal): Promise<LoadedLayer> {
  const { buf } = await benchBytes(compareArtifactPath(rid, l.path), signal)
  if (l.kind === 'points') return { kind: 'points', data: decodePointLayer(buf, l) }
  const m = await parseMeshGlb(buf)
  return { kind: 'mesh', root: m.root, faces: m.faces }
}

/** 图层查询的合并结果（模块级函数，TanStack Query 在各查询结果不变时复用上一次的返回值） */
function combineLayers(rs: readonly UseQueryResult<LoadedLayer>[]) {
  return { data: rs.map((r) => r.data), pending: rs.some((r) => r.isPending && r.fetchStatus !== 'idle'), failed: rs.map((r) => r.isError) }
}

const fmt = (d: number) => (v: number) => (Number.isFinite(v) ? v.toFixed(d) : '—')
const pct = (v: number | null | undefined, d = 1) => (v === null || v === undefined || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(d)}%`)
const intText = (n: number) => n.toLocaleString('en-US')

function srcPatch(next: unknown[]): string | null {
  const s = SOURCES.filter((x) => next.includes(x))
  return s.length === SOURCES.length ? null : s.join(',') || 'none'
}

function MainStats({ doc, uav, mode }: { doc: CompareDoc; uav: string; mode: (typeof MODES)[number] }) {
  const t = useT()
  const m = (k: Parameters<typeof medianOf>[3]) => medianOf(doc, uav, mode, k) ?? Number.NaN
  const fa = m('fscore@0.2')
  const fb = m('fscore@0.2_aligned')
  const n = metricValues(doc, uav, mode, 'rpe100_pct').length
  return (
    <div className="flex flex-col gap-2" data-compare-stats={`${uav}/${mode}`}>
      <span className="text-hud-sub text-muted-foreground" data-compare-cond="">
        {t('lab.lidarCompare.stats.cond', {
          mode: t(`lab.lidarCompare.mode.${mode}`), obs: doc.obs ?? '—', seeds: doc.seeds ?? '—', repeats: doc.repeats ?? '—', n,
        })}
      </span>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2">
        <LfStat label={t('lab.lidarCompare.stats.rpe')} unit="%" value={m('rpe100_pct')} format={fmt(2)} />
        <LfStat label={t('lab.lidarCompare.stats.ate')} unit="m" value={m('ate_aligned_rmse_m')} format={fmt(2)} />
        <LfStat label={t('lab.lidarCompare.stats.drift')} unit="m" value={m('drift_max_m')} format={fmt(2)} />
        <LfStat label={t('lab.lidarCompare.stats.f02')} value={fa} format={(v) => (Number.isFinite(v) ? `${v.toFixed(2)} / ${Number.isFinite(fb) ? fb.toFixed(2) : '—'}` : '—')} />
        <LfStat label={t('lab.lidarCompare.stats.colorMae')} unit="DN" value={m('color_mae_dn')} format={fmt(1)} />
      </div>
    </div>
  )
}

function FleetPanel({ doc }: { doc: CompareDoc }) {
  const t = useT()
  const l0 = doc.l0
  if (!l0) return <span className="text-hud-sub text-muted-foreground">{t('lab.lidarCompare.fleet.none')}</span>
  const ok = l0.jaccardMax !== null ? l0.jaccardMax <= l0.threshold : null
  return (
    <div className="flex flex-col gap-2" data-compare-fleet="" data-jaccard-max={l0.jaccardMax ?? ''} data-jaccard-ok={ok === null ? '' : String(ok)}>
      <div className="grid grid-cols-2 gap-x-4">
        <LfStat label={t('lab.lidarCompare.fleet.merged')} unit="%" value={(l0.merged ?? Number.NaN) * 100} format={fmt(1)} />
        <LfStat label={t('lab.lidarCompare.fleet.jaccardMax')} value={l0.jaccardMax ?? Number.NaN} format={fmt(3)} />
      </div>
      <span className="text-hud-sub text-muted-foreground">
        {t(ok === false ? 'lab.lidarCompare.fleet.over' : 'lab.lidarCompare.fleet.within', { th: l0.threshold })}
        {' · '}
        {l0.uavs.map((u) => `${u} ${pct(l0.coverage[u])}`).join(' · ')}
      </span>
      <LfMatrix labels={l0.uavs} matrix={l0.matrix} threshold={l0.threshold} format={fmt(2)} ariaLabel={t('lab.lidarCompare.fleet.matrix')} />
    </div>
  )
}

function EvalDistance({ doc, uav }: { doc: CompareDoc; uav: string }) {
  const t = useT()
  const d = doc.l0?.meshDistance[uav]
  if (!d) return null
  const m = (v: number | null) => (v === null ? '—' : `${v.toFixed(3)} m`)
  return (
    <span className="font-mono text-hud-sub text-muted-foreground tabular-nums" data-compare-evalsim={uav}>
      {t('lab.lidarCompare.evalSim', { median: m(d.medianM), p90: m(d.p90M), max: m(d.maxM), within: pct(d.withinHalfDiag) })}
    </span>
  )
}

function ErrStatsLine({ s, label }: { s: ErrStats & { points?: number }; label: string }) {
  const t = useT()
  const mm = (v: number) => (Number.isFinite(v) ? `${v.toFixed(3)} m` : '—')
  return (
    <span className="font-mono text-hud-sub tabular-nums" data-compare-rect-n={s.n}>
      {t('lab.lidarCompare.rect.stats', { label, n: intText(s.n), none: intText(s.none), mean: mm(s.meanM), median: mm(s.medianM), p90: mm(s.p90M), max: mm(s.maxM) })}
    </span>
  )
}

export function LidarComparePage({ params, search }: RouteProps) {
  const t = useT()
  const session = useBenchSession()
  const rid = params.rid ?? ''
  const q = parseCompareSearch(search)
  useDocTitle(t('lab.lidarCompare.docTitle'))
  const canRead = session !== null && ROLE_RANK[session.role] >= ROLE_RANK.researcher
  const docQ = useQuery({
    queryKey: compareDocKey(rid), queryFn: async ({ signal }) => parseCompareDoc(await benchGet<unknown>(compareArtifactPath(rid, COMPARE_DOC), signal)),
    enabled: canRead && rid !== '', retry: false, retryOnMount: false, staleTime: Infinity,
  })
  // 显示档探测在首次渲染时执行一次（探测上下文立即释放）；Node 中的静态渲染没有 document，保持待定
  const [probe] = React.useState<CompareTier | null | 'pending'>(() => (typeof document === 'undefined' ? 'pending' : probeCompareTier()))
  const tier: CompareTier | null = q.tier ?? (probe === 'pending' || probe === null ? null : probe)
  const doc = docQ.data
  const srcKey = q.src.join(',')
  const sel = React.useMemo(() => (doc ? resolveSelection(doc, q) : null), [doc, q.uav, q.mode, q.rep]) // eslint-disable-line react-hooks/exhaustive-deps
  const want = React.useMemo(() => (doc && sel && tier ? layersFor(doc, sel, tier, q.src) : []), [doc, sel, tier, srcKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const loaded = useQueries({
    queries: want.map((l) => ({
      queryKey: compareLayerKey(rid, l.path), queryFn: ({ signal }: { signal: AbortSignal }) => loadLayer(rid, l, signal), staleTime: Infinity, retry: 1,
      enabled: canRead && !(l.kind === 'mesh' && !meshWithinBudget(l.faces)),
    })),
    combine: combineLayers,
  })
  const loadedData = loaded.data
  const pending = loaded.pending
  const failed = want.filter((_, i) => loaded.failed[i]).map((l) => l.path)

  // 显示层与预算：点层按显示档核对（Tier S 合计、Tier B 每路），网格按面数核对
  const view = React.useMemo(() => {
    const pts: DecodedPoints[] = []
    let mesh: Object3D | null = null
    let meshFaces = 0
    let meshOver = false
    want.forEach((l, i) => {
      const d = loadedData[i]
      if (l.kind === 'mesh') {
        if (!meshWithinBudget(l.faces) || (d?.kind === 'mesh' && !meshWithinBudget(d.faces))) meshOver = true
        else if (d?.kind === 'mesh') {
          mesh = d.root
          meshFaces = d.faces
        }
      } else if (d?.kind === 'points') pts.push(d.data)
    })
    const strides = tier ? budgetStrides(pts.map((p) => p.n), tier) : pts.map(() => 1)
    const shown = pts.map((p, i) => (strides[i] === p.stride ? p : { ...p, stride: strides[i]! }))
    return { points: shown, mesh: mesh as Object3D | null, meshFaces, meshOver }
  }, [tier, want, loadedData])
  const shownPts = view.points.reduce((a, p) => a + shownCount(p.n, p.stride), 0)
  const thinned = view.points.some((p) => p.stride > 1)
  const errCheck = React.useMemo(() => {
    if (!doc) return null
    const m = new Map(view.points.filter((p) => p.err).map((p) => [p.desc.path, p]))
    return checkErrSamples(doc.errSamples.filter((s) => m.has(s.layer)), m)
  }, [doc, view.points])

  // 点选、框选与段定位随所选机、模式、重复与显示档清空（按选择键派生，不在副作用中重置）
  const selKey = `${sel?.uav}/${sel?.mode}/${sel?.rep}/${tier}`
  const [inter, setInter] = React.useState<Interaction>({ key: '', pick: null, rect: null, seg: null })
  const cur = inter.key === selKey ? inter : { key: selKey, pick: null, rect: null, seg: null }
  const patch = (x: Partial<Omit<Interaction, 'key'>>) => setInter({ ...cur, ...x })
  const pick = cur.pick
  const rect = cur.rect
  const focusSeg = cur.seg
  const [box, bw] = useElementWidth<HTMLDivElement>(1200)
  const { h: wh } = useWindowSize()
  const canvasH = Math.max(320, Math.round(wh - 420))
  const focus: Vec3 | null = focusSeg !== null ? (sel?.run?.segments[focusSeg]?.at ?? null) : null

  if (session !== null && !canRead) {
    return (
      <section data-view="lab-lidar-compare" className="app-layer-overlay-page bg-background p-4">
        <PanelEmpty title={t('lab.lidarCompare.roleDenied')} description={t('lab.lidarCompare.roleDeniedHint')}
          action={<Button size="sm" variant="outline" onClick={() => navigate('/lab/board')}>{t('lab.lidarCompare.toBoard')}</Button>} />
      </section>
    )
  }
  const docMissing = docQ.error instanceof BenchError && docQ.error.status === 404
  const docInvalid = docQ.error instanceof CompareDocError
  const header = (
    <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
      <Button size="sm" variant="ghost" onClick={() => navigate(`/lab/runs/${encodeURIComponent(rid)}`)}>
        <Icon icon="chev.left" data-icon="inline-start" />
        {t('lab.lidarCompare.back')}
      </Button>
      <Icon icon="sensor.lidar" />
      <h1 className="text-ed-title font-bold">{t('lab.lidarCompare.title')}</h1>
      <span className="font-mono text-hud-sub">{sanitizeText(rid, 128)}</span>
      {doc ? (
        <span className="ml-auto font-mono text-hud-sub text-muted-foreground" data-compare-vehicle="">
          {[doc.vehicle, doc.worldId, sel ? t(`lab.lidarCompare.mode.${sel.mode}`) : null].filter(Boolean).join(' · ')}
        </span>
      ) : null}
      {tier ? <Badge variant="outline" className="font-mono" data-compare-tier={tier}>{t(`lab.lidarCompare.tier.${tier}`)}</Badge> : null}
    </header>
  )
  if (!doc || !sel) {
    return (
      <section data-view="lab-lidar-compare" className="app-layer-overlay-page flex flex-col bg-background">
        {header}
        <div className="p-4">
          {docQ.isPending && canRead ? <PanelEmpty title={t('lab.lidarCompare.loading')} brand={false} />
            : docMissing ? <PanelEmpty title={t('lab.lidarCompare.missing')} description={t('lab.lidarCompare.missingHint')} />
              : docInvalid ? <PanelEmpty title={t('lab.lidarCompare.invalid')} description={t('lab.lidarCompare.invalidHint')} />
                : docQ.isError ? <PanelEmpty title={t('lab.lidarCompare.unavailable')} description={t('lab.unreachableHint')} /> : null}
        </div>
      </section>
    )
  }

  const run = sel.run
  const budgetText = tier === 'S'
    ? t('lab.lidarCompare.budget.s', { n: intText(shownPts), max: intText(TIER_S_POINTS_TOTAL) })
    : t('lab.lidarCompare.budget.b', { n: intText(shownPts), max: intText(TIER_B_POINTS_PER_MAP), faces: intText(view.meshFaces), fmax: intText(TIER_B_MESH_FACES) })
  const errVal = pick?.layer.err ? errU8ToM(pick.layer.err[pick.hit.i]!) : Number.NaN
  const world = (p: Vec3) => p.map((v, i) => (v + doc.origin[i]!).toFixed(2)).join(', ')
  const panelW = bw >= 1100 ? 400 : bw
  const canvasW = Math.max(240, bw >= 1100 ? bw - panelW - 16 : bw)
  return (
    <section data-view="lab-lidar-compare" data-figure="lab-lidar-compare" className="app-layer-overlay-page flex flex-col bg-background">
      {header}
      <div className="flex flex-wrap items-end gap-3 border-b px-4 py-2" data-figure="lab-lidar-compare-controls">
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.lidarCompare.sources')}</span>
          <ToggleGroup multiple spacing={0} size="sm" variant="outline" value={[...q.src]} aria-label={t('lab.lidarCompare.sources')} data-compare-src={q.src.join(',')}
            onValueChange={(v: unknown[]) => updateSearch({ src: srcPatch(v) })}>
            {SOURCES.map((s) => <ToggleGroupItem key={s} value={s} data-compare-src-item={s}>{t(`lab.lidarCompare.src.${s}`)}</ToggleGroupItem>)}
          </ToggleGroup>
        </label>
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.lidarCompare.colorBy')}</span>
          <NativeSelect size="sm" value={q.color} data-compare-color={q.color} onChange={(e) => updateSearch({ color: e.target.value === 'err' ? null : e.target.value })}>
            {COLOR_BY.map((c) => <NativeSelectOption key={c} value={c}>{t(`lab.lidarCompare.color.${c}`)}</NativeSelectOption>)}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1 text-hud-sub">
          <span className="text-muted-foreground">{t('lab.lidarCompare.pointSize')}</span>
          <NativeSelect size="sm" value={String(q.size)} onChange={(e) => updateSearch({ size: e.target.value === '2' ? null : e.target.value })}>
            {POINT_SIZES.map((s) => <NativeSelectOption key={s} value={String(s)}>{`${s} px`}</NativeSelectOption>)}
          </NativeSelect>
        </label>
        {doc.crop ? (
          <Toggle size="sm" variant="outline" pressed={q.crop} onPressedChange={(v: boolean) => updateSearch({ crop: v ? null : '0' })} data-compare-crop={doc.crop.id}>
            <Icon icon="tool.clip" data-icon="inline-start" />
            {t('lab.lidarCompare.crop', { id: doc.crop.id })}
          </Toggle>
        ) : null}
        <div className="ml-auto flex flex-col items-end gap-0.5">
          <ErrLegend />
          <EvalDistance doc={doc} uav={sel.uav} />
          {errCheck && errCheck.n ? (
            <span className={`font-mono text-hud-sub tabular-nums ${errCheck.ok ? 'text-muted-foreground' : 'text-brand-text'}`}
              data-err-check-n={errCheck.n} data-err-check-max-m={errCheck.maxAbsDiffM ?? ''} data-err-check-ok={String(errCheck.ok)}>
              <Icon icon={errCheck.ok ? 'check' : 'alert.warning'} />
              {t(errCheck.ok ? 'lab.lidarCompare.errCheck.ok' : 'lab.lidarCompare.errCheck.over', {
                n: intText(errCheck.n), d: ((errCheck.maxAbsDiffM ?? 0) * 1000).toFixed(2),
              })}
            </span>
          ) : null}
        </div>
      </div>
      <div ref={box} className="min-h-0 flex-1 overflow-auto p-4">
        <div className={bw >= 1100 ? 'flex gap-4' : 'flex flex-col gap-4'}>
          <div className="flex min-w-0 flex-col gap-2">
            {tier === null ? (
              <div style={{ width: canvasW, height: canvasH }} className="flex items-center justify-center">
                <PanelEmpty brand={false} title={t(probe === null ? 'lab.lidarCompare.noGl' : 'lab.lidarCompare.probing')}
                  description={probe === null ? t('lab.lidarCompare.noGlHint') : undefined} />
              </div>
            ) : (
              <CompareCanvas tier={tier} points={view.points} mesh={view.mesh} trajGt={run?.trajGt ?? null} trajEst={run?.trajEst ?? null} crop={doc.crop}
                showCrop={q.crop} colorBy={q.color} sizePx={q.size} focus={focus} width={canvasW} height={canvasH} ariaLabel={t('lab.lidarCompare.canvasAria')}
                onPick={(v) => patch({ pick: v })} onRect={(v) => patch({ rect: v })} />
            )}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-hud-sub" data-compare-layers={want.map((l) => `${l.src}:${l.kind}`).join(',')}
              data-compare-shown={shownPts} data-compare-thinned={String(thinned)}>
              <span className="font-mono tabular-nums text-muted-foreground" data-compare-budget={tier ?? ''}>{tier ? budgetText : ''}</span>
              {thinned ? <span className="text-muted-foreground" data-compare-note="thinned">{t('lab.lidarCompare.thinned')}</span> : null}
              {view.meshOver ? <span data-compare-note="mesh-over">{t('lab.lidarCompare.meshOver', { max: intText(TIER_B_MESH_FACES) })}</span> : null}
              {pending ? <span className="text-muted-foreground">{t('lab.lidarCompare.layersLoading')}</span> : null}
              {failed.length ? <span data-compare-note="failed">{t('lab.lidarCompare.layersFailed', { n: failed.length })}</span> : null}
              {SOURCES.filter((s) => q.src.includes(s) && !want.some((l) => l.src === s)).map((s) => (
                <span key={s} className="text-muted-foreground" data-compare-note={`absent-${s}`}>{t('lab.lidarCompare.absent', { src: t(`lab.lidarCompare.src.${s}`) })}</span>
              ))}
              <span className="text-muted-foreground">{t('lab.lidarCompare.hint')}</span>
            </div>
            {pick ? (
              <span className="font-mono text-hud-sub tabular-nums" data-compare-pick={`${pick.layer.desc.src}/${pick.hit.i}`}>
                {t('lab.lidarCompare.pick', { src: t(`lab.lidarCompare.src.${pick.layer.desc.src}`), xyz: world(pick.xyz), err: Number.isFinite(errVal) ? `${errVal.toFixed(3)} m` : '—' })}
              </span>
            ) : null}
            {rect ? <ErrStatsLine s={rect} label={t('lab.lidarCompare.rect.label', { n: intText(rect.points) })} /> : null}
          </div>
          <aside className="flex shrink-0 flex-col gap-3" style={{ width: panelW }} data-figure="lab-lidar-compare-panel">
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1 text-hud-sub">
                <span className="text-muted-foreground">{t('lab.lidarCompare.uav')}</span>
                <NativeSelect size="sm" value={sel.uav} data-compare-uav={sel.uav} onChange={(e) => updateSearch({ uav: e.target.value, rep: null })}>
                  {doc.uavs.map((u) => <NativeSelectOption key={u} value={u}>{u}</NativeSelectOption>)}
                </NativeSelect>
              </label>
              <label className="flex flex-col gap-1 text-hud-sub">
                <span className="text-muted-foreground">{t('lab.lidarCompare.modeLabel')}</span>
                <ToggleGroup spacing={0} size="sm" variant="outline" value={[sel.mode]} aria-label={t('lab.lidarCompare.modeLabel')} data-compare-mode={sel.mode}
                  onValueChange={(v: unknown[]) => (MODES as readonly unknown[]).includes(v[0]) && updateSearch({ mode: v[0] as string, rep: null })}>
                  {MODES.map((m) => <ToggleGroupItem key={m} value={m} disabled={!doc.modes.includes(m)}>{t(`lab.lidarCompare.mode.${m}`)}</ToggleGroupItem>)}
                </ToggleGroup>
              </label>
              <label className="flex flex-col gap-1 text-hud-sub">
                <span className="text-muted-foreground">{t('lab.lidarCompare.rep')}</span>
                <NativeSelect size="sm" value={String(sel.rep ?? '')} data-compare-rep={sel.rep ?? ''} disabled={!sel.reps.length} onChange={(e) => updateSearch({ rep: e.target.value })}>
                  {sel.reps.map((r) => <NativeSelectOption key={r} value={String(r)}>{`r${r}`}</NativeSelectOption>)}
                </NativeSelect>
              </label>
            </div>
            {run && !run.ok ? (
              <span className="text-hud-sub" data-compare-run-failed={run.code ?? ''}>{t('lab.lidarCompare.runFailed', { code: run.code ?? '—' })}</span>
            ) : null}
            <LfChartCard title={t('lab.lidarCompare.stats.title')} src={t('lab.lidarCompare.stats.src', { v: doc.evalVersion ?? '—' })}>
              <MainStats doc={doc} uav={sel.uav} mode={sel.mode} />
            </LfChartCard>
            <LfChartCard title={t('lab.lidarCompare.box.title')} sub={t('lab.lidarCompare.box.sub')} figureId="lab-lidar-compare-box">
              <LfBox groups={doc.modes.map((m) => ({ key: m, label: t(`lab.lidarCompare.mode.${m}`), stats: boxStats(metricValues(doc, sel.uav, m, 'rpe100_pct')) }))}
                band={doc.rpeBand} bandLabel={t('lab.lidarCompare.box.band')} format={fmt(2)} ariaLabel={t('lab.lidarCompare.box.title')} />
            </LfChartCard>
            <LfChartCard title={t('lab.lidarCompare.fleet.title')} sub={t('lab.lidarCompare.fleet.sub')} figureId="lab-lidar-compare-fleet">
              <FleetPanel doc={doc} />
            </LfChartCard>
          </aside>
        </div>
        <div className="mt-4">
          <LfChartCard title={t('lab.lidarCompare.curve.title')} sub={run ? t('lab.lidarCompare.curve.sub', { uav: sel.uav, mode: t(`lab.lidarCompare.mode.${sel.mode}`), rep: run.rep }) : undefined}
            figureId="lab-lidar-compare-curve">
            <SegmentCurve segments={run?.segments ?? []} focused={focusSeg} onFocus={(i) => patch({ seg: i })} />
          </LfChartCard>
        </div>
      </div>
    </section>
  )
}
