// Replay a WAAPI animation on demand (M15 §7.1.4): returns a ref and a play() that runs `keyframes` with the token
// timing; skipped in reduced and off.
import * as React from 'react'
import { getMotionTier } from './tier'

export function useReplay<T extends HTMLElement>(keyframes: Keyframe[], timing: KeyframeAnimationOptions): [React.RefObject<T | null>, () => void] {
  const ref = React.useRef<T>(null)
  const play = React.useCallback(() => {
    const tier = getMotionTier()
    if (tier === 'reduced' || tier === 'off') return
    ref.current?.animate(keyframes, timing)
  }, [keyframes, timing])
  return [ref, play]
}
