// LiDAR 与重建对比的指标图（AWR-14 §21.8.3；lieflat 图表规范 AWR-15 §9）：箱线图（LfBox 图型：箱为四分位，须为 p10–p90，点为单次，
// 区间阈值以参考带显示）、两两 Jaccard 灰阶矩阵（LfMatrix 图型：灰阶格、数值文字）、误差色阶图例与沿程曲线（LfHairlineLine，点击段定位画布）。
// 箱线图与矩阵在 ui/lf 尚无对应组件（§21.17.3 新增图型），先在本页内实现，样式只用 lf-* 类与主题令牌；不使用色相与红色表达数值大小。
import { useT } from '@/app/i18n'
import { Button } from '@/ui/components/ui/button'
import { LfHairlineLine } from '@/ui/lf/LfLine'
import { ERR_CAP_M, type BoxStats, type Segment } from './compare'

export interface BoxGroup { key: string; label: string; stats: BoxStats | null }
export interface LfBoxProps {
  groups: readonly BoxGroup[]
  /** 参考带（例如 D3-AC-14 realistic 档 RPE100 中位数区间） */
  band?: readonly [number, number] | null
  bandLabel?: string
  format: (v: number) => string
  width?: number
  rowH?: number
  ariaLabel: string
}

/** 横向箱线图：每组一行，共用一条数值轴 */
export function LfBox({ groups, band, bandLabel, format, width = 360, rowH = 34, ariaLabel }: LfBoxProps) {
  const padL = 44
  const padR = 12
  const top = band ? 16 : 6
  const h = top + groups.length * rowH + 20
  let lo = Number.POSITIVE_INFINITY
  let hi = Number.NEGATIVE_INFINITY
  for (const g of groups) {
    if (!g.stats) continue
    lo = Math.min(lo, g.stats.min)
    hi = Math.max(hi, g.stats.max)
  }
  if (band) {
    lo = Math.min(lo, band[0])
    hi = Math.max(hi, band[1])
  }
  if (!Number.isFinite(lo)) {
    lo = 0
    hi = 1
  }
  if (!(hi > lo)) hi = lo + 1
  const span = hi - lo
  lo -= span * 0.04
  hi += span * 0.04
  const x = (v: number) => padL + ((v - lo) / (hi - lo)) * (width - padL - padR)
  const base = top + groups.length * rowH
  return (
    <svg className="lf-svg" viewBox={`0 0 ${width} ${h}`} width="100%" role="img" aria-label={ariaLabel} data-lf-box="">
      {band ? (
        <g data-lf-band={`${band[0]},${band[1]}`} aria-hidden="true">
          <rect className="lf-track" x={x(band[0])} y={top} width={Math.max(1, x(band[1]) - x(band[0]))} height={groups.length * rowH} />
          {bandLabel ? <text x={x(band[1])} y={top - 4} textAnchor="end" fontSize={9.5} className="lf-label">{bandLabel}</text> : null}
        </g>
      ) : null}
      <line className="lf-grid" x1={padL} x2={width - padR} y1={base} y2={base} aria-hidden="true" />
      <text x={padL} y={base + 13} fontSize={9.5} className="lf-axis">{format(lo)}</text>
      <text x={width - padR} y={base + 13} textAnchor="end" fontSize={9.5} className="lf-axis">{format(hi)}</text>
      {groups.map((g, i) => {
        const cy = top + i * rowH + rowH / 2
        const s = g.stats
        return (
          <g key={g.key} data-box-group={g.key} data-box-n={s?.n ?? 0}
            data-box-q={s ? [s.p10, s.q1, s.median, s.q3, s.p90].map((v) => v.toPrecision(12)).join(',') : ''}>
            <text x={padL - 6} y={cy + 3} textAnchor="end" fontSize={10} className="lf-label">{g.label}</text>
            {s ? (
              <>
                <line className="lf-line-faint" x1={x(s.p10)} x2={x(s.p90)} y1={cy} y2={cy} />
                <line className="lf-line-faint" x1={x(s.p10)} x2={x(s.p10)} y1={cy - 5} y2={cy + 5} />
                <line className="lf-line-faint" x1={x(s.p90)} x2={x(s.p90)} y1={cy - 5} y2={cy + 5} />
                <rect className="lf-hollow" x={x(s.q1)} y={cy - 8} width={Math.max(1, x(s.q3) - x(s.q1))} height={16} />
                <line className="lf-line" x1={x(s.median)} x2={x(s.median)} y1={cy - 8} y2={cy + 8} />
                {s.values.map((v, k) => <circle key={k} className="lf-solid" cx={x(v)} cy={cy + 12} r={1.6}><title>{format(v)}</title></circle>)}
                <text x={x(s.median)} y={cy - 11} textAnchor="middle" fontSize={9.5} className="lf-value lf-halo">{format(s.median)}</text>
              </>
            ) : null}
          </g>
        )
      })}
    </svg>
  )
}

/** 两两 Jaccard 灰阶矩阵：格深随数值（0–1）加深；对角为"—"；超过阈值的格以粗体标出 */
export function LfMatrix({ labels, matrix, threshold, format, ariaLabel }: { labels: readonly string[]; matrix: readonly (readonly number[])[]; threshold: number; format: (v: number) => string; ariaLabel: string }) {
  return (
    <table className="border-collapse font-mono text-hud-sub tabular-nums" aria-label={ariaLabel} data-lf-matrix="">
      <thead>
        <tr>
          <th />
          {labels.map((l) => <th key={l} className="px-2 py-1 text-right font-semibold text-muted-foreground">{l}</th>)}
        </tr>
      </thead>
      <tbody>
        {labels.map((r, i) => (
          <tr key={r}>
            <th className="px-2 py-1 text-left font-semibold text-muted-foreground">{r}</th>
            {labels.map((c, j) => {
              const v = matrix[i]?.[j] ?? Number.NaN
              const diag = i === j
              const pct = diag || !Number.isFinite(v) ? 0 : Math.round(Math.min(1, Math.max(0, v)) * 100)
              return (
                <td key={c} data-jaccard={diag ? '' : Number.isFinite(v) ? v.toFixed(4) : 'nan'}
                  className={`px-2 py-1 text-right ${!diag && v > threshold ? 'font-extrabold' : ''}`}
                  style={{ backgroundColor: `color-mix(in oklab, var(--lf-data) ${Math.round(pct * 0.6)}%, var(--lf-bg))` }}>
                  {diag ? '—' : Number.isFinite(v) ? format(v) : '—'}
                </td>
              )
            })}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** 误差色阶图例：0 m（g400）至 0.5 m（g50），超出按 0.5 m 显示 */
export function ErrLegend() {
  const t = useT()
  return (
    <div className="flex items-center gap-2 text-hud-sub" data-compare-legend="">
      <span className="text-muted-foreground">{t('lab.lidarCompare.legend.title')}</span>
      <span className="font-mono tabular-nums">0</span>
      <span aria-hidden="true" className="h-2 w-32 rounded-sm" style={{ backgroundImage: 'linear-gradient(to right, var(--g400), var(--g50))' }} />
      <span className="font-mono tabular-nums">{`${ERR_CAP_M} m`}</span>
      <span className="text-muted-foreground">{t('lab.lidarCompare.legend.clip')}</span>
    </div>
  )
}

const CURVE_W = 400
const CURVE_PAD_X = 20
/** 沿程曲线：每 100 m 段的相对平移误差（%）与锚定漂移（m）；点击段在画布中定位该段起点 */
export function SegmentCurve({ segments, onFocus, focused }: { segments: readonly Segment[]; onFocus: (i: number) => void; focused: number | null }) {
  const t = useT()
  const xl = segments.map((s) => (s.sM / 1000).toFixed(1))
  const n = segments.length
  const rpe = segments.map((s, i) => ({ t: i, v: s.rpePct ?? Number.NaN })).filter((p) => Number.isFinite(p.v))
  const drift = segments.map((s, i) => ({ t: i, v: s.driftM ?? Number.NaN })).filter((p) => Number.isFinite(p.v))
  const xPct = (i: number) => ((CURVE_PAD_X + (n <= 1 ? 0 : (i / (n - 1)) * (CURVE_W - 2 * CURVE_PAD_X))) / CURVE_W) * 100
  if (!n) return <span className="text-hud-sub text-muted-foreground">{t('lab.lidarCompare.curve.empty')}</span>
  const overlay = (
    <div className="absolute inset-0">
      {segments.map((s, i) => (
        <Button key={i} size="icon-xs" variant="ghost" data-segment={i} data-segment-focused={focused === i ? '' : undefined} disabled={!s.at}
          aria-label={t('lab.lidarCompare.curve.focus', { s: s.sM.toFixed(0) })} aria-pressed={focused === i}
          className="absolute top-0 h-full w-3 min-w-0 -translate-x-1/2 aria-pressed:bg-muted"
          style={{ left: `${xPct(i)}%` }}
          onClick={() => onFocus(i)} />
      ))}
    </div>
  )
  return (
    <div className="grid grid-cols-1 gap-2 md:grid-cols-2" data-compare-curve={n}>
      {[{ key: 'rpe', data: rpe, unit: '%', fmt: (v: number) => v.toFixed(2) }, { key: 'drift', data: drift, unit: 'm', fmt: (v: number) => v.toFixed(2) }].map((c) => (
        <div key={c.key} className="flex flex-col gap-1">
          <span className="text-hud-sub text-muted-foreground">{t(`lab.lidarCompare.curve.${c.key}`)}</span>
          <div className="relative">
            <LfHairlineLine data={c.data.length === n ? c.data : segments.map((_, i) => c.data.find((p) => p.t === i) ?? { t: i, v: 0, hollow: true })}
              xLabels={xl} width={CURVE_W} height={120} hero="none" ariaLabel={t(`lab.lidarCompare.curve.${c.key}`)} format={c.fmt} />
            {overlay}
          </div>
        </div>
      ))}
    </div>
  )
}
