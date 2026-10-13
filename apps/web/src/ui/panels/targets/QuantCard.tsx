// Perception readout card (R-D2-40; ADR-124; AWR-04 §15.10.4; AWR-14 §20.26.7; M15-FR-238; UX-FR-160). Owner: M15 (the
// readings are M19's `target/{id}/quant`, 2 Hz, phone 1 Hz, mirrored in stores/perception `quant`).
// One lieflat table.log row per vehicle of the selected target (<= 12, by slant range): vehicle, slant range, acoustic
// "dominant band level / band threshold" (their difference is the margin), margin, acoustic probability, visual "angular size
// / detection threshold" (arcmin), equivalent pixels on the target's eye, visual probability, combined probability and the
// verdict of the current criterion mode (geometric: only the configured bodies decide; the probabilities are a physical
// reference). A row click focuses the camera on the vehicle and highlights its solid shell in the field view; the selected
// row's full sentence ("P600-02 -> person-01 · 182 m · acoustic 30.9 dB(A) / 33.0 · margin -2.1 dB · P 0.26 · visual 8.2'
// (8.2 px) / 1.0' · P 1.00 · verdict: not met") is shown under the table. The probability header carries a Popover:
// "display probabilities; the verdict follows the deterministic criterion". Viewers read it too (no writes).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { usePerception, type PairReading, type QuantFrame } from '@/stores/perception'
import { camera, field } from '@/viewport/facade'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/ui/components/ui/popover'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf'
import { PanelEmpty } from '@/ui/brand'
import { sanitizeText } from '@/lib/sanitize'
import { useElementWidth } from '@/ui/layout/useElementWidth'

/** below this card width the table shows the compact columns (the 360 px detail rail, phones); the row sentence has the rest */
export const QUANT_COMPACT_PX = 600
/** the columns of the compact table (d2-WP-13b-to-M15 item 6: the full table was cut after "margin" in the detail rail):
 *  the decibel level against its threshold and the angular size against its threshold (R-D2-40) and the verdict, which
 *  fit the 360 px rail; the slant range, the margin, the probabilities and the equivalent pixels are in the row sentence */
export const QUANT_COMPACT_KEYS: readonly string[] = ['uav', 'ac', 'vis', 'verdict']
/** whether the card shows the compact columns (0: not measured yet, the full table) */
export function quantCompact(widthPx: number, allCols: boolean): boolean {
  return widthPx > 0 && widthPx < QUANT_COMPACT_PX && !allCols
}

const n1 = (v: number | null | undefined): string => (v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(1))
const p2 = (v: number | null | undefined): string => (v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(2))

/** the readout sentence of one pair (AWR-04 §15.10.4 example) */
export function pairSentence(t: (k: string, p?: Record<string, string | number>) => string, q: QuantFrame, p: PairReading, targetLabel: string): string {
  const parts = [`${p.uav} → ${targetLabel}`, t('quant.range', { m: fmt.num(p.range_m, 0) })]
  if (p.acoustic) {
    parts.push(t('quant.acousticText', { lp: n1(p.acoustic.lp_band_db), thr: n1(p.acoustic.thr_dba) }))
    parts.push(t('quant.marginText', { m: n1(p.acoustic.margin_db) }))
    parts.push(t('quant.pText', { p: p2(p.acoustic.p) }))
  }
  if (p.visual) {
    parts.push(t('quant.visualText', { th: n1(p.visual.theta_arcmin), px: n1(p.visual.n_eq_px), thr: n1(p.visual.theta_th_arcmin) }))
    parts.push(t('quant.pText', { p: p2(p.visual.p) }))
  }
  if (p.radar) parts.push(t('quant.radarText', { snr: n1(p.radar.snr_db), req: n1(p.radar.snr_req_db), p: p2(p.radar.p) }))
  const verdict = t(p.would_discover ? 'quant.verdict.met' : 'quant.verdict.unmet')
  const why = !p.would_discover && q.criterion_mode === 'geometric' ? t('quant.geometricNote') : ''
  parts.push(`${t('quant.verdict')}${verdict}${why}`)
  return parts.join(' · ')
}

function ProbHeader() {
  const t = useT()
  return (
    <Popover>
      <PopoverTrigger render={<Button size="xs" variant="ghost" className="h-auto gap-1 px-0 font-medium" data-quant-prob-note="" />}>
        {t('quant.p')}
        <Icon icon="alert.info" className="size-3" />
      </PopoverTrigger>
      <PopoverContent className="w-64 text-hud-sub">{t('quant.probNote')}</PopoverContent>
    </Popover>
  )
}

/** the card: an always-mounted wrapper measures the width (the readout arrives after the first render) */
export function QuantCard(props: { targetId: string | null; targetLabel: string; hasVehicles?: boolean }) {
  const [ref, width] = useElementWidth<HTMLDivElement>(0)
  return <div ref={ref} className="min-w-0"><QuantBody {...props} width={width} /></div>
}

function QuantBody({ targetId, targetLabel, hasVehicles = true, width }: { targetId: string | null; targetLabel: string; hasVehicles?: boolean; width: number }) {
  const t = useT()
  const q = usePerception((s) => (s.quant && s.quant.target === targetId ? s.quant : null))
  const [sel, setSel] = React.useState<string | null>(null)
  const [allCols, setAllCols] = React.useState(false)
  React.useEffect(() => () => field.highlight(null), [])
  if (!targetId) return <PanelEmpty brand={false} title={t('quant.empty.noTarget')} />
  if (!hasVehicles) return <PanelEmpty brand={false} title={t('quant.empty.noVehicles')} />
  if (!q) return <p className="text-hud-sub text-muted-foreground" data-quant-card="waiting">{t('quant.waiting')}</p>
  if (q.pairs.length === 0) return <PanelEmpty brand={false} title={t('quant.empty.noVehicles')} />
  const geometric = q.criterion_mode === 'geometric'
  const narrow = quantCompact(width, false)
  const compact = quantCompact(width, allCols)
  const all: LfColumn<PairReading>[] = [
    { key: 'uav', label: t('quant.col.uav'), format: (r) => sanitizeText(r.uav, 24) },
    { key: 'range', label: t('quant.col.range'), unit: 'm', align: 'right', format: (r) => fmt.num(r.range_m, 0) },
    { key: 'ac', label: t('quant.col.acoustic'), unit: 'dB(A)', align: 'right', format: (r) => (r.acoustic ? `${n1(r.acoustic.lp_band_db)} / ${n1(r.acoustic.thr_dba)}` : '—') },
    { key: 'margin', label: t('quant.col.margin'), unit: 'dB', align: 'right', format: (r) => (r.acoustic ? n1(r.acoustic.margin_db) : '—') },
    { key: 'pac', label: t('quant.col.pAc'), align: 'right', format: (r) => p2(r.acoustic?.p) },
    { key: 'vis', label: t('quant.col.visual'), unit: '′', align: 'right', format: (r) => (r.visual ? `${n1(r.visual.theta_arcmin)} / ${n1(r.visual.theta_th_arcmin)}` : '—') },
    { key: 'px', label: t('quant.col.px'), unit: 'px', align: 'right', format: (r) => n1(r.visual?.n_eq_px) },
    { key: 'pvis', label: t('quant.col.pVis'), align: 'right', format: (r) => p2(r.visual?.p) },
    { key: 'pany', label: t('quant.col.pAny'), align: 'right', format: (r) => p2(r.p_any) },
    { key: 'verdict', label: t('quant.col.verdict'), format: (r) => t(r.would_discover ? 'quant.verdict.met' : 'quant.verdict.unmet') },
  ]
  const columns = compact ? all.filter((c) => QUANT_COMPACT_KEYS.includes(c.key)) : all
  const pick = (r: PairReading): void => {
    const next = sel === r.uav ? null : r.uav
    setSel(next)
    field.highlight(next)
    if (next) camera.focus([next])
  }
  const chosen = q.pairs.find((p) => p.uav === sel) ?? null
  return (
    <div className="flex min-w-0 flex-col gap-2" data-quant-card={q.pairs.length} data-quant-mode={q.criterion_mode} data-quant-cols={compact ? 'compact' : 'all'}>
      <div className="flex flex-wrap items-center gap-1.5 text-hud-sub">
        <Badge variant="outline" data-quant-mode-badge="">{t(`quant.mode.${q.criterion_mode}`)}</Badge>
        {geometric ? <span className="text-muted-foreground">{t('quant.physicalRef')}</span> : null}
        <span className="ml-auto flex items-center gap-1">
          {narrow ? (
            <Button size="xs" variant="ghost" aria-pressed={allCols} onClick={() => setAllCols((x) => !x)} data-quant-cols-toggle="">
              {t(allCols ? 'quant.cols.compact' : 'quant.cols.all')}
            </Button>
          ) : null}
          <ProbHeader />
        </span>
      </div>
      <div className="min-w-0 overflow-x-auto">
        <LfTable columns={columns} rows={q.pairs} rowKey={(r) => r.uav} selected={sel} onRowClick={pick} height={Math.min(360, 44 + q.pairs.length * 28)}
          rowHeight={28} ariaLabel={t('quant.title')} figureId="quant-card" className={compact ? undefined : 'min-w-[44rem]'} />
      </div>
      {chosen ? <p className="text-hud-sub leading-relaxed" data-quant-sentence={chosen.uav}>{pairSentence(t, q, chosen, targetLabel)}</p>
        : <p className="text-hud-sub text-muted-foreground">{t('quant.rowHint')}</p>}
      <p className="text-hud-cap text-muted-foreground">{t('field.freeSpace')}</p>
    </div>
  )
}
