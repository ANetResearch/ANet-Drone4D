// Toasts (M15-FR-089; g07 §6 item 6): Base UI Toast through shadcn toast.tsx, limit 3 (the 4th gets data-limited),
// timeouts from INPUT (info 4 s, warning 6 s, critical 0 = sticky). The merge helper updates a toast with the same
// merge key (source:type:reason) instead of adding another one. Delivery (ADR-069; D1-AC-27): notify() only records the
// latest content of a key; the toast manager is called after the next frame was presented, in an idle period (or after
// IDLE_TIMEOUT_MS), and a live toast is updated at most once per TOAST_UPDATE_MIN_MS (its text still shows the latest
// merged count; a level change goes out at once). Base UI's ToastContent re-measures its root with a MutationObserver
// on every text change (style.height = auto, offsetHeight, flushSync): called from the 4 Hz event bridge flush it
// forced a layout of everything React had just committed in the same task (event table, rails), 20-50 ms per flush on
// Tier S during a 1000-vehicle storm and the main source of our > 50 ms LoAF. In an idle period after a presented frame
// the layout is clean, so the measurement only lays out the toast.
import type * as React from 'react'
import { Toaster, toast } from '@/ui/components/ui/toast'
import { INPUT, LIMITS } from '@/lib/tokens/input.gen'
import { sanitizeText } from '@/lib/sanitize'
import { UX } from '@/ui/testing/uxProbe'

export function ToastProvider({ children }: { children: React.ReactNode }) {
  return (
    <Toaster limit={LIMITS.toastLimit} timeout={INPUT.toastInfoMs}>
      {children}
    </Toaster>
  )
}

export type ToastLevel = 'info' | 'warning' | 'critical' | 'success'
/** a live toast is rewritten at most this often (its merged count is still the latest one when it is) */
export const TOAST_UPDATE_MIN_MS = 1000

interface ToastData { title: string; description: string | undefined; type: string; timeout: number }
interface Slot { live: boolean; lastMs: number; shown: ToastData | null; pending: ToastData | null; timer: ReturnType<typeof setTimeout> | null }
const slots = new Map<string, Slot>()
const dirty = new Set<string>()
let scheduled = false

const now = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now())
const same = (a: ToastData | null, b: ToastData): boolean =>
  a !== null && a.title === b.title && a.description === b.description && a.type === b.type && a.timeout === b.timeout

/** idle wait cap: the toast still goes out within this time when the main thread never idles (ms) */
const IDLE_TIMEOUT_MS = 300

/**
 * run `cb` after the next presented frame, in an idle period when there is one (rAF, then requestIdleCallback with a
 * timeout): in an idle period nothing has dirtied the layout since that frame, so Base UI's height measurement lays out
 * the toast only. A macrotask where there is no rAF or the page is hidden.
 */
function afterNextPaint(cb: () => void): void {
  if (typeof requestAnimationFrame !== 'function' || (typeof document !== 'undefined' && document.hidden)) {
    setTimeout(cb, 0)
    return
  }
  requestAnimationFrame(() => {
    if (typeof requestIdleCallback === 'function') requestIdleCallback(() => cb(), { timeout: IDLE_TIMEOUT_MS })
    else setTimeout(cb, 0)
  })
}

function schedule(): void {
  if (scheduled) return
  scheduled = true
  afterNextPaint(deliver)
}

/**
 * one delivery: at most one toast write (add or update) per call, the remaining dirty keys go to the next idle period.
 * Every write re-renders the toast list and makes Base UI measure the toast (layout plus a flushSync render); several in
 * one task added up to 40-60 ms of our script in one animation frame during a storm (D1-AC-27).
 */
function deliver(): void {
  scheduled = false
  const t = now()
  for (const key of dirty) {
    dirty.delete(key)
    const s = slots.get(key)
    if (!s || !s.pending) continue
    const data = s.pending
    if (!s.live) {
      s.live = true
      s.lastMs = t
      s.shown = data
      s.pending = null
      toast.add({ id: key, ...data, onRemove: () => drop(key) })
      break
    }
    if (same(s.shown, data)) {
      s.pending = null
      continue
    }
    const wait = s.lastMs + TOAST_UPDATE_MIN_MS - t
    if (wait > 0 && s.shown?.type === data.type) {
      // too soon after the last write: keep the latest content and come back when the interval is over
      if (!s.timer) {
        s.timer = setTimeout(() => {
          s.timer = null
          dirty.add(key)
          schedule()
        }, wait)
      }
      continue
    }
    s.lastMs = t
    s.shown = data
    s.pending = null
    toast.update(key, data)
    break
  }
  if (dirty.size) schedule()
}

function drop(key: string): void {
  const s = slots.get(key)
  if (s?.timer) clearTimeout(s.timer)
  slots.delete(key)
  dirty.delete(key)
}

/** add or update the toast of a merge key; texts are sanitised (M15-FR-108) */
export function notify(key: string, level: ToastLevel, title: string, description?: string): void {
  const timeout = level === 'critical' ? 0 : level === 'warning' ? INPUT.toastWarnMs : INPUT.toastInfoMs
  const type = level === 'critical' ? 'error' : level === 'warning' ? 'warning' : level === 'success' ? 'success' : 'info'
  const data: ToastData = { title: sanitizeText(title), description: description ? sanitizeText(description) : undefined, type, timeout }
  UX.toasts.merged[key] = (UX.toasts.merged[key] ?? 0) + 1
  let s = slots.get(key)
  if (!s) {
    s = { live: false, lastMs: 0, shown: null, pending: null, timer: null }
    slots.set(key, s)
  }
  s.pending = data
  dirty.add(key)
  schedule()
}

/** deliver every pending toast now (tests; normally one write per idle period after a presented frame) */
export function flushToasts(): void {
  while (dirty.size) deliver()
}
