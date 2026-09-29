// Resident loop slot for a DOM element (M15-FR-057; ADR-029): asks ui/motion/budget for a slot while the element is on
// screen (IntersectionObserver) and the tier allows loops; releases it when hidden or unmounted. Without
// IntersectionObserver the element never counts as on screen, so decorative loops stay off.
import * as React from 'react'
import { motionBudget, type LoopHandle } from './budget'
import { useMotionTier } from './tier'

export function useLoopSlot(ref: React.RefObject<HTMLElement | null>, priority: number): boolean {
  const [on, setOn] = React.useState(false)
  const tier = useMotionTier()
  React.useEffect(() => {
    const el = ref.current
    if (!el || tier === 'reduced' || tier === 'off' || typeof IntersectionObserver !== 'function') return
    const h: LoopHandle = {
      priority,
      onScreen: false,
      pause: () => setOn(false),
      resume: () => setOn(true),
    }
    const io = new IntersectionObserver((entries) => {
      h.onScreen = entries.some((e) => e.isIntersecting)
      if (h.onScreen) setOn(motionBudget.requestLoop(h))
      else {
        motionBudget.releaseLoop(h)
        setOn(false)
      }
    })
    io.observe(el)
    return () => {
      io.disconnect()
      motionBudget.releaseLoop(h)
      setOn(false)
    }
  }, [ref, priority, tier])
  return on && tier !== 'reduced' && tier !== 'off'
}
