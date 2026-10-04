// ADR-081 (M15-FR-089, FR-004; D1-AC-27, D1-AC-03a) in a real browser with the app stylesheet: the page toaster's viewport
// is resident before any toast, a contained fixed-size compositor layer (will-change) that keeps the toast's corner where
// it was; a notice is written in the post-layout slot of the next frame (not in the caller's task), the toast animates
// transform and opacity only, its content keeps its own height, and the stack variables do not reach the toast's
// children; without a surface (?chrome=0) nothing reaches the DOM and the notice is kept in the non-DOM channel.
import '@/styles/index.css'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ToastProvider, flushToasts, headlessNotices, notify, setSurface } from '@/app/providers/ToastProvider'
import { installLayoutClock } from '@/ui/notify/afterLayout'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  installLayoutClock()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  setSurface(true)
})

const viewport = (): HTMLElement | null => document.querySelector('[data-slot="toast-viewport"]')
const toasts = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('[data-slot="toast"]')]
const frame = (): Promise<void> => new Promise((ok) => requestAnimationFrame(() => setTimeout(ok, 0)))

describe('page toaster surface (ADR-081)', () => {
  it('is resident, contained and its own layer before the first toast', async () => {
    await act(async () => root.render(<ToastProvider surface><div>app</div></ToastProvider>))
    await act(frame)
    const vp = viewport()
    expect(vp).not.toBeNull()
    expect(vp!.parentElement?.getAttribute('data-slot')).toBe('toast-portal')
    const cs = getComputedStyle(vp!)
    expect(cs.contain).toBe('strict')
    expect(cs.willChange).toBe('transform')
    expect(cs.position).toBe('fixed')
    // the toast column (22.5 rem) plus the 1.5 rem bleed on both sides; the stack area 24 rem plus the bleed
    expect(Number.parseFloat(cs.width)).toBeCloseTo(16 * (22.5 + 3), 0)
    expect(Number.parseFloat(cs.height)).toBeCloseTo(16 * (24 + 3), 0)
    expect(toasts()).toHaveLength(0)
  })

  it('writes a notice in the post-layout slot of the next frame; transform and opacity only', async () => {
    await act(async () => root.render(<ToastProvider surface><div>app</div></ToastProvider>))
    await act(frame)
    notify('test:surface', 'warning', 'first toast', 'second line')
    expect(toasts()).toHaveLength(0)            // never inside the caller's task
    await act(async () => {
      for (let i = 0; i < 4 && toasts().length === 0; i++) await frame()
    })
    const [t] = toasts()
    expect(t).toBeDefined()
    expect(t.textContent).toContain('first toast')
    const ts = getComputedStyle(t)
    expect(ts.transitionProperty.split(',').map((x) => x.trim()).sort()).toEqual(['opacity', 'transform'])
    expect(ts.filter).toBe('none')
    // the toast keeps its corner 1.5 rem inside the viewport box (12 px inside the unobscured rect); layout offsets, the
    // entry transform may still run
    const vp = viewport()!
    expect(vp.clientWidth - (t.offsetLeft + t.offsetWidth)).toBe(24)
    expect(vp.clientHeight - (t.offsetTop + t.offsetHeight)).toBe(24)
    // content height is its own (auto), and the stack variables stay on the root
    const content = t.querySelector<HTMLElement>('[data-slot="toast-content"]')!
    expect(content.style.height).toBe('')
    expect(getComputedStyle(content).getPropertyValue('--toast-index').trim()).toBe('')
    expect(getComputedStyle(content).getPropertyValue('--scale').trim()).toBe('')
    expect(getComputedStyle(t).getPropertyValue('--gap').trim()).not.toBe('')
    expect(getComputedStyle(content).getPropertyValue('--gap').trim()).not.toBe('')
  })

  it('without a surface (?chrome=0) mounts no viewport and keeps notices off the DOM', async () => {
    await act(async () => root.render(<ToastProvider surface={false}><div>app</div></ToastProvider>))
    await act(frame)
    expect(viewport()).toBeNull()
    const before = headlessNotices().length
    notify('governor', 'info', 'floor released')
    flushToasts()
    await act(frame)
    expect(viewport()).toBeNull()
    expect(toasts()).toHaveLength(0)
    expect(headlessNotices().length).toBe(Math.min(before + 1, 16))
    expect(headlessNotices().at(-1)?.title).toBe('floor released')
  })
})
