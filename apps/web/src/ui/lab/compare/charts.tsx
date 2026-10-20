// 档间对照的图（AWR-14 §21.10；lieflat 图表规范 AWR-15 §9）：两档轨迹平面图（快速仿真实线 --lf-data，高保真仿真虚线 --lf-data2）、
// 时间曲线（高度、位置差，共用 lab 播放头的竖线）与指标差带（每行一条容差带 [0, tol]，差值为点；超出容差的点为本图唯一的红色强调）。
// 轨迹与曲线的路径只在数据变化时重建（memo），播放头只移动标记；样式只用 lf-* 类与主题令牌，不使用色相表达数值大小。
import * as React from 'react'
import { plotIndices, STATE_N, type DiffRow } from './tierCompare'

const REAL_STYLE = { stroke: 'var(--lf-data2)' } as const
const DASH = '5 3'

function pathOf(xs: readonly number[], ys: readonly number[]): string {
  let d = ''
  for (let i = 0; i < xs.length; i++) d += `${i ? 'L' : 'M'}${xs[i]!.toFixed(1)} ${ys[i]!.toFixed(1)}`
  return d
}

export interface PlanProps {
  fast: Float64Array | null
  real: Float64Array | null
  n: number
  /** 播放头样本下标（-1 为无） */
  idx: number
  extent: { e: readonly [number, number]; n: readonly [number, number] }
  width: number
  height: number
  ariaLabel: string
  axisLabel: string
}

/** 平面投影（E 向右、N 向上，两轴等比例） */
function planScale(extent: PlanProps['extent'], width: number, height: number, pad = 12) {
  const se = extent.e[1] - extent.e[0]
  const sn = extent.n[1] - extent.n[0]
  const k = Math.min((width - 2 * pad) / se, (height - 2 * pad) / sn)
  const ox = (width - k * se) / 2
  const oy = (height - k * sn) / 2
  return { x: (e: number) => ox + (e - extent.e[0]) * k, y: (n: number) => height - oy - (n - extent.n[0]) * k }
}

const PlanPaths = React.memo(function PlanPaths({ fast, real, n, extent, width, height }: Omit<PlanProps, 'idx' | 'ariaLabel' | 'axisLabel'>) {
  const sc = planScale(extent, width, height)
  const ix = plotIndices(n)
  const line = (s: Float64Array | null) => (s ? pathOf(ix.map((i) => sc.x(s[i * STATE_N]!)), ix.map((i) => sc.y(s[i * STATE_N + 1]!))) : '')
  return (
    <g aria-hidden="true">
      {real ? <path className="lf-line" style={REAL_STYLE} strokeDasharray={DASH} d={line(real)} data-track="realistic" data-track-points={ix.length} /> : null}
      {fast ? <path className="lf-line" d={line(fast)} data-track="fast" data-track-points={ix.length} /> : null}
      {fast && n ? <rect className="lf-solid" x={sc.x(fast[0]!) - 2.5} y={sc.y(fast[1]!) - 2.5} width={5} height={5} /> : null}
    </g>
  )
})

export function TrackPlan({ fast, real, n, idx, extent, width, height, ariaLabel, axisLabel }: PlanProps) {
  const sc = planScale(extent, width, height)
  const mark = (s: Float64Array | null) => (s && idx >= 0 && idx < n ? [sc.x(s[idx * STATE_N]!), sc.y(s[idx * STATE_N + 1]!)] as const : null)
  const mf = mark(fast)
  const mr = mark(real)
  return (
    <svg className="lf-svg" viewBox={`0 0 ${width} ${height}`} width="100%" role="img" aria-label={ariaLabel} data-compare-plan={n}>
      <line className="lf-floor" x1={0} x2={width} y1={height - 0.5} y2={height - 0.5} aria-hidden="true" />
      <PlanPaths fast={fast} real={real} n={n} extent={extent} width={width} height={height} />
      {mr ? <circle className="lf-hollow" style={REAL_STYLE} cx={mr[0]} cy={mr[1]} r={4} data-mark="realistic" /> : null}
      {mf ? <circle className="lf-solid" cx={mf[0]} cy={mf[1]} r={3} data-mark="fast" /> : null}
      <text x={4} y={height - 6} fontSize={9.5} className="lf-axis">{axisLabel}</text>
    </svg>
  )
}

export interface TimeSeries { key: string; values: Float64Array; kind: 'fast' | 'realistic' | 'diff' }
export interface TimeChartProps {
  series: readonly TimeSeries[]
  tS: Float64Array
  idx: number
  refLine?: { v: number; label: string } | null
  format: (v: number) => string
  width: number
  height?: number
  ariaLabel: string
  figure: string
}

function yDomain(series: readonly TimeSeries[], ix: readonly number[], ref: number | null): readonly [number, number] {
  let lo = Infinity
  let hi = -Infinity
  for (const s of series) {
    for (const i of ix) {
      const v = s.values[i]!
      if (!Number.isFinite(v)) continue
      if (v < lo) lo = v
      if (v > hi) hi = v
    }
  }
  if (ref !== null) {
    lo = Math.min(lo, ref)
    hi = Math.max(hi, ref)
  }
  if (!Number.isFinite(lo)) return [0, 1]
  if (series.every((s) => s.kind === 'diff')) lo = Math.min(0, lo)
  const span = hi - lo || Math.max(Math.abs(hi), 1) * 0.1
  return [lo - span * 0.06, hi + span * 0.06]
}

const PAD_L = 4
const PAD_B = 14
const TimePaths = React.memo(function TimePaths({ series, tS, width, height, dom }: { series: readonly TimeSeries[]; tS: Float64Array; width: number; height: number; dom: readonly [number, number] }) {
  const n = tS.length
  const ix = plotIndices(n)
  const t0 = n ? tS[0]! : 0
  const t1 = n ? tS[n - 1]! : 1
  const x = (t: number) => PAD_L + ((t - t0) / (t1 - t0 || 1)) * (width - 2 * PAD_L)
  const y = (v: number) => height - PAD_B - ((v - dom[0]) / (dom[1] - dom[0])) * (height - PAD_B - 6)
  return (
    <g aria-hidden="true">
      {series.map((s) => {
        const pts = ix.filter((i) => Number.isFinite(s.values[i]!))
        const d = pathOf(pts.map((i) => x(tS[i]!)), pts.map((i) => y(s.values[i]!)))
        return <path key={s.key} className="lf-line" d={d} style={s.kind === 'realistic' ? REAL_STYLE : undefined} strokeDasharray={s.kind === 'realistic' ? DASH : undefined}
          data-series={s.key} data-series-points={pts.length} />
      })}
    </g>
  )
})

export function TimeChart({ series, tS, idx, refLine, format, width, height = 132, ariaLabel, figure }: TimeChartProps) {
  const n = tS.length
  const ix = React.useMemo(() => plotIndices(n), [n])
  const dom = React.useMemo(() => yDomain(series, ix, refLine?.v ?? null), [series, ix, refLine?.v])
  const t0 = n ? tS[0]! : 0
  const t1 = n ? tS[n - 1]! : 1
  const x = (t: number) => PAD_L + ((t - t0) / (t1 - t0 || 1)) * (width - 2 * PAD_L)
  const y = (v: number) => height - PAD_B - ((v - dom[0]) / (dom[1] - dom[0])) * (height - PAD_B - 6)
  const base = height - PAD_B
  return (
    <svg className="lf-svg" viewBox={`0 0 ${width} ${height}`} width="100%" role="img" aria-label={ariaLabel} data-compare-chart={figure} data-chart-samples={n}>
      <line className="lf-floor" x1={PAD_L} x2={width - PAD_L} y1={base + 0.5} y2={base + 0.5} aria-hidden="true" />
      {refLine ? (
        <g aria-hidden="true" data-chart-ref={refLine.v}>
          <line className="lf-ref" x1={PAD_L} x2={width - PAD_L} y1={y(refLine.v)} y2={y(refLine.v)} />
          <text x={width - PAD_L} y={y(refLine.v) - 3} textAnchor="end" fontSize={9.5} className="lf-label lf-halo">{refLine.label}</text>
        </g>
      ) : null}
      <TimePaths series={series} tS={tS} width={width} height={height} dom={dom} />
      {idx >= 0 && idx < n ? <line className="lf-line-faint" x1={x(tS[idx]!)} x2={x(tS[idx]!)} y1={6} y2={base} data-chart-playhead={idx} /> : null}
      <text x={PAD_L} y={height - 2} fontSize={9.5} className="lf-axis">{`${t0.toFixed(1)} s`}</text>
      <text x={width - PAD_L} y={height - 2} textAnchor="end" fontSize={9.5} className="lf-axis">{`${t1.toFixed(1)} s`}</text>
      <text x={PAD_L} y={12} fontSize={9.5} className="lf-axis">{format(dom[1])}</text>
    </svg>
  )
}

export interface DiffBandsProps {
  rows: readonly DiffRow[]
  label: (key: string) => string
  value: (row: DiffRow, v: number | null) => string
  width?: number
  rowH?: number
  ariaLabel: string
}

/** 指标差带：每行的刻度独立（0 至 max(1.25·tol, 1.05·diff)），带为 [0, tol]，竖线为容差，点为档间差 */
export function DiffBands({ rows, label, value, width = 420, rowH = 30, ariaLabel }: DiffBandsProps) {
  const padL = 150
  const padR = 170
  const h = rows.length * rowH + 4
  return (
    <svg className="lf-svg" viewBox={`0 0 ${width} ${h}`} width="100%" role="img" aria-label={ariaLabel} data-compare-bands={rows.length}>
      {rows.map((r, i) => {
        const cy = i * rowH + rowH / 2
        const xmax = Math.max(r.tol * 1.25, (r.diff ?? 0) * 1.05) || 1
        const x = (v: number) => padL + (Math.min(Math.max(v, 0), xmax) / xmax) * (width - padL - padR)
        const out = !r.recomputed
        return (
          <g key={r.key} data-band-key={r.key} data-band-diff={r.diff ?? ''} data-band-tol={r.tol} data-band-within={String(r.recomputed)}
            data-band-consistent={String(r.consistent)}>
            <text x={padL - 8} y={cy + 3} textAnchor="end" fontSize={10} className="lf-label">{label(r.key)}</text>
            <rect className="lf-track" x={padL} y={cy - 6} width={Math.max(1, x(r.tol) - padL)} height={12} />
            <line className="lf-ref" x1={x(r.tol)} x2={x(r.tol)} y1={cy - 9} y2={cy + 9} />
            {r.diff !== null ? <circle className={out ? 'lf-hero' : 'lf-solid'} cx={x(r.diff)} cy={cy} r={out ? 4 : 3.2} /> : null}
            <text x={width - padR + 8} y={cy + 3} fontSize={10} className={out ? 'lf-value' : 'lf-label'}>{value(r, r.diff)}</text>
          </g>
        )
      })}
    </svg>
  )
}
