// Frame cap coordination (M15-FR-007, §6.3.4): overlay pages ask for 5 fps, modals for 15 fps, the small-window Empty
// suspends the viewport; with several sources the smallest non-zero cap wins, so closing a modal never lifts the cap of
// an overlay page that is still open.
import { viewport } from '@/viewport/facade'

const caps = new Map<string, number>()
const suspenders = new Set<string>()
const changed = new Set<() => void>()

function apply(): void {
  let cap = 0
  for (const v of caps.values()) if (v > 0 && (cap === 0 || v < cap)) cap = v
  viewport.setFrameCap(cap)
  viewport.setSuspended(suspenders.size > 0)
  for (const cb of changed) cb()
}
export const frameCap = {
  set(source: string, fps: number): void {
    if (fps > 0) caps.set(source, fps)
    else caps.delete(source)
    apply()
  },
  suspend(source: string, on: boolean): void {
    if (on) suspenders.add(source)
    else suspenders.delete(source)
    apply()
  },
  /** is the viewport suspended by this source (the power-save mode freezes more than the loop, ui/lf/scheduler.ts) */
  suspendedBy(source: string): boolean {
    return suspenders.has(source)
  },
  /** after every cap or suspension change; returns the unsubscribe */
  onChange(cb: () => void): () => void {
    changed.add(cb)
    return () => {
      changed.delete(cb)
    }
  },
}
