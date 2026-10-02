// ADR-069 in a real browser: a folded panel holds its store state (no re-render on store writes, one render when it
// becomes visible again), and the warm stage stays display:none until the pre-raster phase shows it, renders specimens
// of every toast level and the 3D cube, turns the cube per phase frame and is hidden again synchronously.
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createAwrStore } from '@/lib/createStore'
import { PanelVisibleContext, useVisibleState } from '@/ui/panels/PanelHost'
import { WarmStage, hideWarmStage, showWarmStage, warmStep } from '@/app/boot/WarmStage'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

describe('useVisibleState (D1-AC-27)', () => {
  it('does not re-render a hidden panel on store writes and catches up when shown', () => {
    const store = createAwrStore('test.visible', () => ({ v: 0 }))
    let renders = 0
    function Probe() {
      const s = useVisibleState(store)
      renders++
      return <span data-v={s.v}>{s.v}</span>
    }
    const view = (visible: boolean) => <PanelVisibleContext value={visible}><Probe /></PanelVisibleContext>
    act(() => root.render(view(true)))
    act(() => store.setState({ v: 1 }))
    expect(host.querySelector('span')!.dataset.v).toBe('1')
    act(() => root.render(view(false)))
    const before = renders
    act(() => {
      store.setState({ v: 2 })
      store.setState({ v: 3 })
    })
    expect(renders).toBe(before)
    expect(host.querySelector('span')!.dataset.v).toBe('1')
    act(() => root.render(view(true)))
    expect(host.querySelector('span')!.dataset.v).toBe('3')
  })
})

describe('WarmStage (ADR-069)', () => {
  it('is display:none until shown, holds every toast level and the cube, hides synchronously', async () => {
    act(() => root.render(<WarmStage />))
    const stage = document.querySelector<HTMLElement>('[data-warm-stage]')!
    expect(stage).not.toBeNull()
    expect(stage.hidden).toBe(true)
    expect(getComputedStyle(stage).display).toBe('none')
    expect(stage.getAttribute('aria-hidden')).toBe('true')
    expect(stage.inert).toBe(true)
    act(() => showWarmStage())
    expect(stage.hidden).toBe(false)
    await act(async () => { await new Promise((r) => setTimeout(r, 50)) })
    expect(stage.querySelectorAll('[data-slot="toast"]').length).toBe(4)
    const faces = stage.querySelectorAll('[class*="backface-visibility"]')
    expect(faces.length).toBe(6)
    const cube = faces[0].parentElement!
    warmStep(3)
    const a = cube.style.transform
    warmStep(4)
    expect(cube.style.transform).not.toBe(a)
    hideWarmStage()
    expect(stage.hidden).toBe(true)
  })
})
