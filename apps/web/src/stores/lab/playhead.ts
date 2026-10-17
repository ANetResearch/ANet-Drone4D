// lab playhead (AWR-14 §21.1 item 3, §21.13): the one time authority of the workspace pages. Replay of a recorded run
// in the internal build is driven by the local clock (no RT session yet in P0a-1); every wall tile, the timeline and
// later the scene view read tS from here and never keep their own clock.
import { create } from 'zustand'

export interface PlayheadState {
  tS: number
  durationS: number
  playing: boolean
  rate: number
  setDuration: (s: number) => void
  seek: (s: number) => void
  play: () => void
  pause: () => void
  setRate: (r: number) => void
  /** advance by a wall-clock delta (seconds); stops at the end */
  advance: (wallDtS: number) => void
}

export const usePlayhead = create<PlayheadState>((set, get) => ({
  tS: 0,
  durationS: 0,
  playing: false,
  rate: 1,
  setDuration: (s) => set({ durationS: Math.max(0, s), tS: Math.min(get().tS, Math.max(0, s)) }),
  seek: (s) => set({ tS: Math.min(Math.max(0, s), get().durationS) }),
  play: () => set({ playing: true, tS: get().tS >= get().durationS ? 0 : get().tS }),
  pause: () => set({ playing: false }),
  setRate: (r) => set({ rate: r }),
  advance: (dt) => {
    const s = get()
    if (!s.playing) return
    const t = s.tS + dt * s.rate
    if (t >= s.durationS) set({ tS: s.durationS, playing: false })
    else set({ tS: t })
  },
}))
