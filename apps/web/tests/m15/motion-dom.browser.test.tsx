// M15-FR-055 (d02 §4.4) in a real browser: MotionNumber replays only the changed digits, holds values inside the
// telemetry interval and assigns directly in reduced tier; SwapText keeps one visible text. BoundText and MotionNumber
// are raster islands (ADR-066: own compositor layer through data-island) unless an enclosing element already is one.
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BoundText } from '@/ui/motion/BoundText'
import { MotionNumber } from '@/ui/motion/MotionNumber'
import { SwapText } from '@/ui/motion/SwapText'
import { motionBudget } from '@/ui/motion/budget'
import { setTestMotion } from '@/ui/motion/tier'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  motionBudget.reset()
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  setTestMotion(null)
})

const fmt = (v: number) => String(Math.round(v))
const digits = () => [...host.querySelectorAll('.t-digit')].map((s) => s.textContent).join('')
const running = () => host.querySelector('.t-number')!.getAnimations({ subtree: true }).length

describe('MotionNumber', () => {
  it('renders the first value without animation, then replays only changed digits', () => {
    setTestMotion('lite')
    act(() => root.render(<MotionNumber value={128} format={fmt} minIntervalMs={0} aria-label="n" />))
    expect(digits()).toBe('128')
    expect(running()).toBe(0)
    act(() => root.render(<MotionNumber value={129} format={fmt} minIntervalMs={0} aria-label="n" />))
    expect(digits()).toBe('129')
    expect(host.querySelector<HTMLElement>('.t-number')!.dataset.value).toBe('129')
    expect(running()).toBe(1)
    act(() => root.render(<MotionNumber value={200} format={fmt} minIntervalMs={0} aria-label="n" />))
    expect(running()).toBeGreaterThanOrEqual(2)
  })

  it('holds values that arrive inside the telemetry interval and applies the latest one', async () => {
    setTestMotion('lite')
    act(() => root.render(<MotionNumber value={1} format={fmt} minIntervalMs={80} />))
    act(() => root.render(<MotionNumber value={2} format={fmt} minIntervalMs={80} />))
    act(() => root.render(<MotionNumber value={3} format={fmt} minIntervalMs={80} />))
    act(() => root.render(<MotionNumber value={4} format={fmt} minIntervalMs={80} />))
    expect(digits()).toBe('2')
    await new Promise((r) => setTimeout(r, 120))
    expect(digits()).toBe('4')
  })

  it('assigns directly in reduced tier', () => {
    setTestMotion('reduced')
    act(() => root.render(<MotionNumber value={10} format={fmt} minIntervalMs={0} />))
    act(() => root.render(<MotionNumber value={11} format={fmt} minIntervalMs={0} />))
    expect(digits()).toBe('11')
    expect(running()).toBe(0)
  })
})

describe('SwapText', () => {
  it('shows the new text and removes the leaving span after the swap', async () => {
    setTestMotion('lite')
    act(() => root.render(<SwapText value="在线" />))
    act(() => root.render(<SwapText value="正在重连" />))
    const spans = () => [...host.querySelectorAll('.t-swap > span')].map((s) => s.textContent)
    expect(spans()).toEqual(['在线', '正在重连'])
    // the leaving span goes when its exit animation ends; poll instead of a fixed wait (flaky under the full parallel
    // run, INT-1 §7.11)
    await vi.waitFor(() => expect(spans()).toEqual(['正在重连']), { timeout: 5000, interval: 50 })
  })

  it('swaps instantly in off tier', () => {
    setTestMotion('off')
    act(() => root.render(<SwapText value="a" />))
    act(() => root.render(<SwapText value="b" />))
    expect([...host.querySelectorAll('.t-swap > span')].map((s) => s.textContent)).toEqual(['b'])
  })
})

describe('raster islands (ADR-066)', () => {
  const read = () => 12.5
  const format = (v: number) => v.toFixed(1)
  it('BoundText and MotionNumber carry data-island by default and drop it with island={false}', () => {
    act(() => root.render(<div><BoundText read={read} format={format} /><MotionNumber value={7} format={fmt} /></div>))
    expect(host.querySelectorAll('[data-island]').length).toBe(2)
    expect(host.querySelector('[data-numeric]')!.textContent).toBe('12.5')
    act(() => root.render(<div data-row=""><BoundText read={read} format={format} island={false} /><MotionNumber value={7} format={fmt} island={false} /></div>))
    expect(host.querySelectorAll('[data-island]').length).toBe(0)
  })

  it('an island rule promotes the span to its own layer (atomic inline box with a transform hint)', () => {
    const style = document.createElement('style')
    style.textContent = '[data-island] { will-change: transform; } span[data-island] { display: inline-block; }'
    document.head.appendChild(style)
    try {
      act(() => root.render(<BoundText read={read} format={format} />))
      const cs = getComputedStyle(host.querySelector('[data-island]')!)
      expect(cs.willChange).toBe('transform')
      expect(cs.display).toBe('inline-block')
    } finally {
      style.remove()
    }
  })
})
