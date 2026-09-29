// M15-FR-068, FR-072 (ADR-031; d01 §3.6): the CPU canvas sparkline and live line draw through the single LfScheduler
// (rAF fallback while the engine loop is not running); hidden charts are not drawn.
import '@/styles/index.css'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LfLine } from '@/ui/lf/LfLine'
import { LfSparkline } from '@/ui/lf/LfSparkline'
import { LfRing } from '@/ui/lf/series'
import { lfScheduler } from '@/ui/lf/scheduler'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  document.documentElement.classList.add('dark')
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

const frames = (n: number) => new Promise<void>((resolve) => {
  let k = 0
  const step = () => (++k >= n ? resolve() : requestAnimationFrame(step))
  requestAnimationFrame(step)
})
function inked(cv: HTMLCanvasElement): number {
  const g = cv.getContext('2d')!
  const { data } = g.getImageData(0, 0, cv.width, cv.height)
  let n = 0
  for (let i = 3; i < data.length; i += 4) if (data[i] > 0) n++
  return n
}
function ring(n: number): LfRing {
  const r = new LfRing(256)
  const t0 = performance.now() - n * 100
  for (let i = 0; i < n; i++) r.push(t0 + i * 100, 30 + 10 * Math.sin(i / 4))
  return r
}

describe('LfSparkline', () => {
  it('draws the series on its canvas', async () => {
    const s = ring(60)
    act(() => root.render(<LfSparkline series={s} width={64} height={16} hz={10} ariaLabel="spark" />))
    await frames(20)
    const cv = host.querySelector('canvas')!
    expect(cv.getAttribute('aria-label')).toBe('spark')
    expect(inked(cv)).toBeGreaterThan(20)
  })

  it('does not draw while its panel is hidden', async () => {
    const s = ring(60)
    act(() => root.render(<LfSparkline series={s} width={64} height={16} hz={10} ariaLabel="hidden" />))
    const cv = host.querySelector('canvas')!
    lfScheduler.setVisible(cv, false)
    await frames(20)
    expect(inked(cv)).toBe(0)
    lfScheduler.setVisible(cv, true)
    s.push(performance.now(), 50)
    await frames(20)
    expect(inked(cv)).toBeGreaterThan(20)
  })
})

describe('LfLine live', () => {
  it('draws a streaming window on a canvas', async () => {
    const s = ring(120)
    act(() => root.render(<LfLine mode="live" series={s} windowSec={20} domain={[0, 60]} target={40} width={320} height={96} hz={10} ariaLabel="live" />))
    await frames(30)
    const cv = host.querySelector('canvas')!
    expect(inked(cv)).toBeGreaterThan(100)
  })
})
