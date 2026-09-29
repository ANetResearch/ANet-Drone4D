// Presence for non Base UI overlays (M15 §7.1.4): keeps the node mounted for `closeMs` after `open` turns false so a
// closing transition can play; reduced and off unmount at once. The phase follows `open` during render (adjusting
// state while rendering); only the delayed 'closing' -> 'closed' step runs in an effect timer.
import * as React from 'react'
import { useMotionTier } from './tier'

type PresencePhase = 'open' | 'closing' | 'closed'

export function usePresence(open: boolean, closeMs: number): { mounted: boolean; phase: PresencePhase } {
  const tier = useMotionTier()
  const instant = tier === 'reduced' || tier === 'off' || closeMs <= 0
  const [phase, setPhase] = React.useState<PresencePhase>(open ? 'open' : 'closed')
  const [prevOpen, setPrevOpen] = React.useState(open)
  if (prevOpen !== open) {
    setPrevOpen(open)
    setPhase(open ? 'open' : instant || phase === 'closed' ? 'closed' : 'closing')
  }
  React.useEffect(() => {
    if (phase !== 'closing') return
    const t = setTimeout(() => setPhase('closed'), instant ? 0 : closeMs)
    return () => clearTimeout(t)
  }, [phase, closeMs, instant])
  return { mounted: phase !== 'closed', phase }
}
