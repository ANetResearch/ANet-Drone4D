// Post-layout slot of a frame (ADR-081; M15-FR-089; D1-AC-27, D1-AC-03a). ResizeObserver callbacks run inside the
// rendering steps of a frame right after style and layout were brought up to date and before paint (HTML "update the
// rendering": style and layout, then resize observations, then paint). A DOM write made there and read back at once
// (Base UI's toast height measurement: style.height = auto + offsetHeight in a layout effect, then flushSync) only lays
// out what that write dirtied, because nothing else in the document is dirty at that moment. The former slot, an idle
// callback after a presented frame, was clean only when the main thread really idled: during a 1000-vehicle storm the
// idle callback mostly ran from its 300 ms timeout after React had committed the event table and the rails, and every
// toast insertion paid a forced style and layout of the whole page (17-31 ms of 25-46 ms, ACC-5 4.3).
// Mechanism: one hidden 1 x 1 sentinel (a direct child of <body>, so every toast element is deeper in the tree and the
// toast's own ResizeObserver is still delivered in the same frame) observed by one ResizeObserver; a request flips the
// sentinel width between 1 and 2 px, which schedules a frame, and the observer callback runs the queued callbacks.
// Order matters: observers are notified in creation order, and an observer that runs before ours in the same frame can
// dirty the layout again (the drone rail's virtualizer re-renders with flushSync from its own ResizeObserver, the shell
// writes the unobscured rect): with the observer created lazily at the first toast, after the shell had mounted, adding a
// toast during a storm still paid 12-25 ms of forced style and layout (FX-TOAST diagnostics). installLayoutClock() is
// therefore called by main.tsx before the app mounts, so this observer is the first of the page. Fallbacks: a macrotask
// where there is no ResizeObserver (Node tests) or the page is hidden (no rendering steps), and a timer when no frame comes
// within AFTER_LAYOUT_FALLBACK_MS.
let sentinel: HTMLElement | null = null
let ro: ResizeObserver | null = null
let wide = false
let pending = false
let fallback: ReturnType<typeof setTimeout> | null = null
const queue: (() => void)[] = []

/** a request is served by a macrotask when no frame ran its resize observations within this time (ms) */
export const AFTER_LAYOUT_FALLBACK_MS = 1000

function runQueue(): void {
  pending = false
  if (fallback !== null) {
    clearTimeout(fallback)
    fallback = null
  }
  const n = queue.length
  for (let i = 0; i < n; i++) {
    const cb = queue.shift()
    try {
      cb?.()
    } catch (e) {
      console.error(e)
    }
  }
}

function ensureObserver(): boolean {
  if (ro && sentinel) {
    // something replaced the body content: put the sentinel back (an observation survives detaching and re-attaching)
    if (!sentinel.isConnected && document.body) document.body.appendChild(sentinel)
    return sentinel.isConnected
  }
  if (typeof ResizeObserver !== 'function' || typeof document === 'undefined' || !document.body) return false
  const el = document.createElement('div')
  el.setAttribute('aria-hidden', 'true')
  el.dataset.slot = 'layout-clock'
  // fixed, invisible and contained: flipping its width lays out this box only and never moves anything else
  el.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;visibility:hidden;pointer-events:none;contain:strict'
  document.body.appendChild(el)
  sentinel = el
  ro = new ResizeObserver(() => {
    if (pending) runQueue()
  })
  ro.observe(el)
  return true
}

/** create the sentinel and its observer now (main.tsx, before the app mounts: the first ResizeObserver of the page) */
export function installLayoutClock(): void {
  ensureObserver()
}

/**
 * run `cb` in the post-layout slot of the next frame (layout clean, before paint); a macrotask when the page is hidden or
 * there is no ResizeObserver. Callbacks queued before the frame run together, in order.
 */
export function afterLayout(cb: () => void): void {
  const hidden = typeof document !== 'undefined' && document.hidden
  if (hidden || !ensureObserver()) {
    setTimeout(cb, 0)
    return
  }
  queue.push(cb)
  if (pending) return
  pending = true
  wide = !wide
  if (sentinel) sentinel.style.width = wide ? '2px' : '1px'
  fallback = setTimeout(() => {
    fallback = null
    if (pending) runQueue()
  }, AFTER_LAYOUT_FALLBACK_MS)
}

// Frame sharing (ADR-081): LoAF sums every script of ours that ran between two renderings, and a toast write always runs
// last in its frame (post-layout slot). Our periodic UI work in the same window (the 4 Hz event bridge flush, up to about
// 21 ms during a storm, and the idle icon warm-up slices, up to about 21 ms) plus a toast add (up to about 36 ms with five
// toasts in the stack) could pass 50 ms together, so that work notes its end here and a toast write waits a frame while it
// ran within UI_WORK_GAP_MS (covers storm frame intervals of up to 150 ms; at most TOAST_MAX_DEFER frames).
let workEnd = Number.NEGATIVE_INFINITY
/** a toast write waits while our periodic UI work ended within this time (ms) */
export const UI_WORK_GAP_MS = 150
const clock = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now())

/** our periodic UI work (event bridge flush, idle warm-up slice) ended now */
export function noteUiWork(): void {
  workEnd = clock()
}

/** true when our periodic UI work ended within the last `ms` */
export function uiWorkWithin(ms: number = UI_WORK_GAP_MS): boolean {
  return clock() - workEnd < ms
}
