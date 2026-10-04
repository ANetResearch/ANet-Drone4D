// Idle plan warm-up (M15-FR-064; d03 §3.5): in requestIdleCallback slices (while timeRemaining() > 3 ms) every whitelisted
// pair is sought at t = 0.5 in both directions on a detached path sink, which fills morphicons' plan cache so the first
// real morph costs <= 0.5 ms. Called once after the boot mask reveals (not on ?chrome=0, which has no icons). A slice yields
// to a pending toast write and notes its end for the frame sharing of toast writes (ADR-081).
import { createMorph } from 'morphicons/dom'
import { ICON_BUCKETS } from '@/lib/tokens/input.gen'
import { MORPH_PAIRS } from './whitelist'
import { toastWritePending } from '@/app/providers/ToastProvider'
import { noteUiWork } from '@/ui/notify/afterLayout'

let started = false
export let prewarmedPairs = 0

export function prewarmIcons(onDone?: () => void): void {
  if (started) return
  started = true
  const sink = { setAttribute() {} }
  let i = 0
  const step = (deadline?: IdleDeadline) => {
    // a toast write waiting for its frame goes first: one pair can take 15-20 ms on Tier S during a storm, and both would
    // count in the same long animation frame (ADR-081); the slice notes its end so the write takes the next frame
    if (deadline && toastWritePending()) {
      schedule()
      return
    }
    const ran = i
    while (i < MORPH_PAIRS.length && (!deadline || deadline.timeRemaining() > ICON_BUCKETS.prewarmSliceMs)) {
      const [a, b] = MORPH_PAIRS[i++]
      const m = createMorph(sink, a as never)
      m.seek(b as never, 0.5)
      m.destroy()
      const n = createMorph(sink, b as never)
      n.seek(a as never, 0.5)
      n.destroy()
      prewarmedPairs++
    }
    if (i > ran) noteUiWork()
    if (i < MORPH_PAIRS.length) schedule()
    else onDone?.()
  }
  const schedule = () => {
    if (typeof requestIdleCallback === 'function') requestIdleCallback(step)
    else setTimeout(() => step(), 0)
  }
  schedule()
}
