// Toasts (M15-FR-089; g07 §6 item 6): Base UI Toast through shadcn toast.tsx, limit 3 (the 4th gets data-limited),
// timeouts from INPUT (info 4 s, warning 6 s, critical 0 = sticky). The merge helper updates a toast with the same
// merge key (source:type:reason) instead of adding another one. Delivery (ADR-069, ADR-081; D1-AC-27, D1-AC-03a):
// notify() only records the latest content of a key; the toast manager is called in the post-layout slot of the next
// frame (ui/notify/afterLayout.ts: inside the rendering steps, after style and layout, before paint), one write per
// frame, and a live toast is updated at most once per TOAST_UPDATE_MIN_MS (its text still shows the latest merged count;
// a level change goes out at once). Base UI measures a toast with a forced layout when it mounts (style.height = auto +
// offsetHeight in a layout effect) and again on every text change (MutationObserver, flushSync); in the post-layout slot
// nothing else is dirty, so the measurement lays out the toast only. The former slot (an idle callback after a presented
// frame, up to 300 ms) was clean only when the main thread really idled: during a storm it mostly ran from its timeout
// after React had committed the page, and each insertion paid a forced style and layout of the whole page (17-31 ms of
// 25-46 ms, ACC-5 4.3).
// Surface (ADR-081): the toaster's viewport is a resident, fixed-size, contained compositor layer (styles/layout.css) and
// toasts animate transform and opacity only (styles/motion/base-ui.css). ?chrome=0 (canvas only, M15-FR-004) mounts no
// toaster at all: notify() then takes the non-DOM channel (headlessNotices(), __ux.toasts.headless) and never touches the
// DOM, so a PerfGovernor step there cannot raise the first toast of the page (one 350-400 ms GPU-process frame on Tier S
// for its first composite, ACC-5 4.2a). The page toaster composes shadcn's toast parts like its Toaster, with the list
// memoized per toast, and only the stack roots restyle on a stack change (styles/layout.css).
import * as React from 'react'
import {
  Toast, ToastAction, ToastClose, ToastContent, ToastDescription, ToastPortal, ToastProvider as ToastRoot, ToastTitle, ToastViewport, toast,
  useToastManager,
} from '@/ui/components/ui/toast'
import { CircleCheckIcon, InfoIcon, OctagonXIcon, TriangleAlertIcon } from '@/ui/icons/lucide-compat'
import { INPUT, LIMITS } from '@/lib/tokens/input.gen'
import { sanitizeText } from '@/lib/sanitize'
import { afterLayout, uiWorkWithin } from '@/ui/notify/afterLayout'
import { parseChrome } from '@/app/router/search'
import { UX } from '@/ui/testing/uxProbe'

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

/**
 * whether the page has a toast surface (false on ?chrome=0): taken from the address at start, so a notice raised before
 * the first render takes the right channel, then written by ToastProvider during render
 */
let surfaceOn = typeof location === 'undefined' || parseChrome(new URLSearchParams(location.search))

type ToastItem = ReturnType<typeof useToastManager>['toasts'][number]

/**
 * the level glyph of shadcn's toast.tsx (same icons and classes) for the four levels notify() raises; the page never
 * raises a loading toast, so the spinner (a resident loop, MOT-03) is not here
 */
function PageToastIcon({ type }: { type: string | undefined }) {
  const glyph = type === 'success' ? <CircleCheckIcon aria-hidden="true" />
    : type === 'info' ? <InfoIcon aria-hidden="true" />
      : type === 'warning' ? <TriangleAlertIcon aria-hidden="true" />
        : type === 'error' ? <OctagonXIcon className="text-destructive" aria-hidden="true" /> : null
  if (!glyph) return null
  return <span data-slot="toast-icon" className="shrink-0 [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-4">{glyph}</span>
}

/**
 * one toast, the composition of shadcn's ToastList, memoized on the toast object (Base UI keeps the object of a toast
 * that did not change): an add, the new toast's height report and the 1 Hz rewrite of one toast re-render that toast's
 * subtree only; the other roots still follow their stack position through their own store subscriptions (ADR-081)
 */
const PageToast = React.memo(function PageToast({ item }: { item: ToastItem }) {
  return (
    <Toast toast={item}>
      <ToastContent>
        <PageToastIcon type={item.type} />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <ToastTitle />
          <ToastDescription />
        </div>
        <ToastAction />
        <ToastClose />
      </ToastContent>
    </Toast>
  )
})

function PageToastList() {
  const { toasts } = useToastManager()
  return toasts.map((item) => <PageToast key={item.id} item={item} />)
}

/** shadcn's Toaster for the page manager (provider, portal, viewport) with the memoized list */
function PageToaster() {
  return (
    <ToastRoot toastManager={toast} limit={LIMITS.toastLimit} timeout={INPUT.toastInfoMs}>
      <ToastPortal>
        <ToastViewport>
          <PageToastList />
        </ToastViewport>
      </ToastPortal>
    </ToastRoot>
  )
}

/**
 * the toaster as a sibling of the app (no toast context is needed by the app itself): toggling the surface mounts or
 * unmounts the toaster only, never the app below it (the resident canvas). `surface` false: no portal, no viewport.
 */
export function ToastProvider({ children, surface = true }: { children: React.ReactNode; surface?: boolean }) {
  if (surfaceOn !== surface) setSurface(surface)
  return (
    <>
      {children}
      {surface ? <PageToaster /> : null}
    </>
  )
}

/** switch the toast surface; a change forgets the live state of every key (a new toaster starts empty) */
export function setSurface(on: boolean): void {
  if (surfaceOn === on) return
  surfaceOn = on
  for (const s of slots.values()) if (s.timer) clearTimeout(s.timer)
  slots.clear()
  dirty.clear()
}

/** a toast write waits at most this many frames for a frame without our periodic UI work (ui/notify/afterLayout.ts) */
export const TOAST_MAX_DEFER = 4
let deferred = 0

function schedule(): void {
  if (scheduled) return
  scheduled = true
  afterLayout(deliver)
}

/** true while a toast write waits for its frame (the idle icon warm-up yields to it) */
export function toastWritePending(): boolean {
  return scheduled
}

/**
 * one delivery: at most one toast write (add or update) per call, the remaining dirty keys go to the next frame's
 * post-layout slot. Every write re-renders the toast list and makes Base UI measure the toast (layout plus a flushSync
 * render); several in one task added up to 40-60 ms of our script in one animation frame during a storm (D1-AC-27).
 */
function deliver(): void {
  scheduled = false
  // frame sharing: our periodic UI work ran in this frame's window, so the write takes the next frame (bounded)
  if (deferred < TOAST_MAX_DEFER && dirty.size && uiWorkWithin()) {
    deferred++
    schedule()
    return
  }
  deferred = 0
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
  if (!surfaceOn) {
    recordHeadless(key, level, data.title)
    return
  }
  let s = slots.get(key)
  if (!s) {
    s = { live: false, lastMs: 0, shown: null, pending: null, timer: null }
    slots.set(key, s)
  }
  s.pending = data
  dirty.add(key)
  schedule()
}

/** one notice of a page without a toast surface (?chrome=0) */
export interface HeadlessNotice { t: number; key: string; level: ToastLevel; title: string }
/** the non-DOM channel keeps this many notices */
export const HEADLESS_KEEP = 16
const headless: HeadlessNotice[] = []

function recordHeadless(key: string, level: ToastLevel, title: string): void {
  headless.push({ t: now(), key, level, title })
  if (headless.length > HEADLESS_KEEP) headless.shift()
  UX.toasts.headless++
}

/** notices raised while the page had no toast surface, oldest first (the last HEADLESS_KEEP; tests, diagnostics) */
export function headlessNotices(): readonly HeadlessNotice[] {
  return headless
}

/** deliver every pending toast now (tests; normally one write per frame, in its post-layout slot) */
export function flushToasts(): void {
  while (dirty.size) deliver()
}
