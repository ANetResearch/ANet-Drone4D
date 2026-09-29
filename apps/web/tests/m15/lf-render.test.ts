// M15-FR-066..079 (ADR-031; d01 §3): lieflat static figures render deterministic SVG markup (LF-CHART-01: seeded jitter,
// no Math.random), the hero is the only red mark, and the chart card keeps the four-part structure. Markup snapshots
// guard the visual language against accidental drift.
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { LfBarRank, barUnit } from '@/ui/lf/LfBarRank'
import { LfChartCard } from '@/ui/lf/LfChartCard'
import { LfLine } from '@/ui/lf/LfLine'
import { LfStat } from '@/ui/lf/LfStat'
import { LfTickGauge } from '@/ui/lf/LfTickGauge'
import { niceStep } from '@/ui/lf/scale'
import { fmt } from '@/lib/format'

const RANK = [
  { label: 'shenzhen', value: 5_000_000 },
  { label: 'hongkong', value: 3_200_000 },
  { label: 'wuhan', value: 2_400_000 },
  { label: 'suzhou', value: 900_000 },
]
const LINE = Array.from({ length: 24 }, (_, i) => ({ t: i, v: 40 + 20 * Math.sin(i / 3) + (i === 17 ? 25 : 0) }))
const count = (s: string, re: RegExp) => (s.match(re) ?? []).length

describe('lieflat static figures', () => {
  it('rung bars: one mark per unit, only the hero bar uses the hero ink', () => {
    const html = renderToStaticMarkup(h(LfBarRank, { variant: 'rung', data: RANK, hero: 'max', format: fmt.pts, ariaLabel: 'rank' }))
    expect(html.startsWith('<svg')).toBe(true)
    expect(html).toContain('aria-label="rank"')
    expect(count(html, /var\(--lf-hero\)/g)).toBeGreaterThan(0)
    expect(count(html, /class="lf-value lf-hero"/g)).toBe(1)
    expect(html).toMatchSnapshot()
  })

  it('tick rows are deterministic between renders', () => {
    const a = renderToStaticMarkup(h(LfBarRank, { variant: 'ticks', data: RANK, format: fmt.pts, ariaLabel: 'ticks' }))
    const b = renderToStaticMarkup(h(LfBarRank, { variant: 'ticks', data: RANK, format: fmt.pts, ariaLabel: 'ticks' }))
    expect(a).toBe(b)
    expect(a).not.toContain('var(--lf-hero)')
    expect(a).toMatchSnapshot()
  })

  it('static hairline line marks the peak as the hero', () => {
    const html = renderToStaticMarkup(h(LfLine, { mode: 'static', data: LINE, hero: 'peak', ariaLabel: 'line', format: (v: number) => fmt.num(v) }))
    expect(html).toContain('<svg')
    expect(html).toMatchSnapshot()
  })

  it('tick gauge shows value and remainder ticks', () => {
    const html = renderToStaticMarkup(h(LfTickGauge, { value: 62, ariaLabel: 'gauge', center: '62%', remainder: '38% left' }))
    expect(html).toContain('aria-label="gauge"')
    expect(html).toMatchSnapshot()
  })

  it('stat and chart card keep the four-part structure', () => {
    const stat = h(LfStat, { label: 'POINTS', value: 4_820_000, format: fmt.pts })
    const html = renderToStaticMarkup(
      h(LfChartCard, { title: 'Shenzhen has the most points', sub: '6 worlds', src: 'WORLD MANIFESTS', figureId: 'test-card', children: stat }),
    )
    expect(html).toContain('data-figure="test-card"')
    expect(html).toContain('data-lf-src')
    expect(html).toContain('4.82M')
    expect(html).toMatchSnapshot()
  })
})

describe('scales', () => {
  it('uses 1-2-2.5-5 nice steps', () => {
    expect(niceStep(0.7)).toBe(1)
    expect(niceStep(3)).toBe(5)
    expect(niceStep(120_000)).toBe(200_000)
    expect(barUnit(RANK)).toBe(200_000)
    expect(barUnit(RANK, 50_000)).toBe(50_000)
  })
})
