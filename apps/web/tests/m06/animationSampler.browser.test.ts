// PerfGovernor step 6 visibility (M06-FR-076; ADR-076): only running animations on the document timeline count. The
// scroll-driven edge fades of .fade-scroll-y (animation-timeline on a scroll container) report playState 'running' as long
// as they exist and never change with html[data-motion]; a time-driven animation or transition does count.
import { afterEach, describe, expect, it } from 'vitest'
import { installAnimationSampler, motionAffected } from '@/viewport/animationSampler'

const nodes: Element[] = []
afterEach(() => {
  for (const n of nodes.splice(0)) n.remove()
})

function scrollDriven(): HTMLElement {
  const scroller = document.createElement('div')
  scroller.style.cssText = 'height: 40px; overflow-y: auto; scroll-timeline: --t y'
  const inner = document.createElement('div')
  inner.style.cssText = 'height: 400px'
  const fade = document.createElement('div')
  fade.style.cssText = 'height: 10px; animation: fx-fade 1ms linear both; animation-timeline: --t'
  scroller.append(fade, inner)
  const style = document.createElement('style')
  style.textContent = '@keyframes fx-fade { from { opacity: 0 } to { opacity: 1 } }'
  document.head.append(style)
  document.body.append(scroller)
  nodes.push(scroller, style)
  return fade
}

describe('animationSampler (ADR-076)', () => {
  it('a scroll-driven animation is running but not motion-affected', () => {
    scrollDriven()
    const all = document.getAnimations()
    expect(all.length).toBeGreaterThan(0)
    expect(all.some((a) => a.playState === 'running')).toBe(true)
    expect(all.some((a) => motionAffected(a))).toBe(false)
  })

  it('a time-driven animation is motion-affected while it runs, not once finished or paused', () => {
    scrollDriven()
    const el = document.createElement('div')
    document.body.append(el)
    nodes.push(el)
    const a = el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 10_000 })
    expect(motionAffected(a)).toBe(true)
    expect(document.getAnimations().some((x) => motionAffected(x))).toBe(true)
    a.pause()
    expect(motionAffected(a)).toBe(false)
    a.finish()
    expect(motionAffected(a)).toBe(false)
  })

  it('the sampler reports no animation with only scroll-driven ones present', async () => {
    scrollDriven()
    const s = installAnimationSampler(50)
    try {
      await new Promise((ok) => setTimeout(ok, 300))
      expect(s.animating).toBe(false)
      const el = document.createElement('div')
      document.body.append(el)
      nodes.push(el)
      const a = el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 10_000 })
      await new Promise((ok) => setTimeout(ok, 300))
      expect(s.animating).toBe(true)
      a.cancel()
    } finally {
      s.dispose()
    }
  })
})
