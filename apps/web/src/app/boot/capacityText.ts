// Capacity texts without the i18n runtime (R-D2-31, R-D2-32; ADR-119, ADR-120; AWR-04 §15.5.3, §15.6.5; AWR-14 §20.26.2 rule
// 3, §20.26.9; M15-FR-225; UX-FR-150). Owner: M15.
// Pure functions over `awr.capacity.v1` (SB-71) bound to a translate function, the DOM writer of the static `.boot-capacity`
// line of index.html and the public boot request. Nothing here imports a store, React or the dictionaries: the early boot
// module (app/boot/early.ts, the first module script of index.html, D2-FX-R2-web) sends the capacity request and writes the
// line with the capacity keys only, before the application chunks have even finished downloading (D2-AC-41: the request
// <= 50 ms after the entry module, the line <= 100 ms after the answer). app/i18n/capacity.ts binds the same functions to
// the full i18n runtime for the landing page, the start Sheet, the queue cards and the application's mask line, so every
// surface shows the same text. Waits round to `< 1 min`, `n min`, `n h`; null is "cannot estimate". The "+1 internal" mark
// follows sandbox.internal_active.
import type { CapacityView } from '@/net/sandbox/client'

export const CAPACITY_PATH = '/api/sandbox/v1/capacity'
export const BOOT_CAPACITY_TIMEOUT_MS = 1500
export const BOOT_CAPACITY_REFRESH_MS = 5000

/** the boot request of the early module handed to the application (window.__awrBootCapacity; app/boot/capacity.ts) */
export interface BootCapacityHandoff {
  /** the request in flight or the last one (the application's store reuses it instead of sending another) */
  current: Promise<CapacityView> | null
  last: CapacityView | null
  failures: number
  /** performance.now() of the last request */
  sentAtMs: number
  stop: () => void
}

export type CapacityKind = 'open' | 'queueViewer' | 'queueSandbox' | 'queueBoth' | 'closed' | 'unavailable'
export interface CapacityLine {
  kind: CapacityKind
  text: string
  /** the "needs queue" badge text (null: no badge) */
  badge: string | null
  viewerQueue: boolean
  sandboxQueue: boolean
}

export type TranslateFn = (key: string, params?: Record<string, string | number>) => string

const n = (v: number | null | undefined): string => (v === null || v === undefined || !Number.isFinite(v) ? '—' : String(v))

/** which side makes a new visitor queue (same rule as stores/capacity queueNeeded) */
export function queueSides(d: CapacityView | null | undefined): { viewer: boolean; sandbox: boolean } {
  if (!d) return { viewer: false, sandbox: false }
  return { viewer: d.viewers?.state === 'full', sandbox: d.sandbox?.state === 'full' || d.sandbox?.state === 'queue' }
}

export interface CapacityTexts {
  /** bare wait text: `< 1 min`, `6 min`, `2 h`; null when it cannot be estimated */
  etaShort: (etaS: number | null | undefined) => string | null
  /** "about 6 min" or "cannot estimate" (queue cards and the viewer line) */
  etaAbout: (etaS: number | null | undefined) => string
  /** "expected about 6 min" or "expected wait cannot be estimated" (the sandbox line) */
  etaExpected: (etaS: number | null | undefined) => string
  /** the text of one capacity response (or of a failed request: `null`) */
  capacityLine: (d: CapacityView | null | undefined) => CapacityLine
  /** the "open my sandbox" button and Sheet hint: "needs queue · position y + 1 · about t" (null: no queue) */
  sandboxQueueHint: (d: CapacityView | null | undefined) => string | null
}

/** the capacity texts over one translate function (the i18n runtime, or the early module's capacity keys) */
export function capacityTexts(t: TranslateFn): CapacityTexts {
  const etaShort = (etaS: number | null | undefined): string | null => {
    if (etaS === null || etaS === undefined || !Number.isFinite(etaS) || etaS < 0) return null
    if (etaS < 60) return t('capacity.eta.lt1')
    if (etaS < 3600) return t('capacity.eta.min', { n: Math.max(1, Math.round(etaS / 60)) })
    return t('capacity.eta.h', { n: Math.max(1, Math.round(etaS / 3600)) })
  }
  const etaAbout = (etaS: number | null | undefined): string => {
    const e = etaShort(etaS)
    return e ? t('capacity.etaAbout', { eta: e }) : t('capacity.eta.unknown')
  }
  const etaExpected = (etaS: number | null | undefined): string => {
    const e = etaShort(etaS)
    return e ? t('capacity.etaExpected', { eta: e }) : t('capacity.etaExpectedUnknown')
  }
  const sandboxCount = (d: CapacityView): string => {
    const internal = (d.sandbox.internal_active ?? 0) > 0 ? ` ${t('capacity.internal')}` : ''
    return `${n(d.sandbox.active)}${internal}`
  }
  const capacityLine = (d: CapacityView | null | undefined): CapacityLine => {
    if (!d || d.schema !== 'awr.capacity.v1') {
      return { kind: 'unavailable', text: t('capacity.unavailable'), badge: null, viewerQueue: false, sandboxQueue: false }
    }
    const q = queueSides(d)
    const closed = d.maintenance || d.sandbox.state === 'closed' || d.hint === 'closed'
    const viewerText = (): string => q.viewer
      ? t('capacity.queueViewer', { live: n(d.viewers.live), max: n(d.viewers.max), pos: (d.viewers.queue ?? 0) + 1, eta: etaAbout(d.viewers.eta_s) })
      : t('capacity.viewers', { live: n(d.viewers.live), max: n(d.viewers.max) })
    if (closed) {
      return { kind: 'closed', text: q.viewer ? `${viewerText()} · ${t('capacity.closed')}` : t('capacity.closed'), badge: q.viewer ? t('capacity.needQueue') : null,
        viewerQueue: q.viewer, sandboxQueue: false }
    }
    const sandboxText = t('capacity.queueSandbox', { active: sandboxCount(d), smax: n(d.sandbox.max), queue: n(d.sandbox.queue), eta: etaExpected(d.sandbox.eta_s) })
    if (q.viewer && q.sandbox) {
      return { kind: 'queueBoth', text: `${viewerText()} · ${sandboxText}`, badge: t('capacity.needQueue'), viewerQueue: true, sandboxQueue: true }
    }
    if (q.viewer) return { kind: 'queueViewer', text: viewerText(), badge: t('capacity.needQueue'), viewerQueue: true, sandboxQueue: false }
    if (q.sandbox) return { kind: 'queueSandbox', text: sandboxText, badge: t('capacity.sandboxNeedsQueue'), viewerQueue: false, sandboxQueue: true }
    return {
      kind: 'open', text: t('capacity.open', { live: n(d.viewers.live), max: n(d.viewers.max), active: sandboxCount(d), smax: n(d.sandbox.max) }),
      badge: null, viewerQueue: false, sandboxQueue: false,
    }
  }
  const sandboxQueueHint = (d: CapacityView | null | undefined): string | null => {
    if (!d || !queueSides(d).sandbox) return null
    return t('capacity.sandboxHint', { pos: (d.sandbox.queue ?? 0) + 1, eta: etaAbout(d.sandbox.eta_s) })
  }
  return { etaShort, etaAbout, etaExpected, capacityLine, sandboxQueueHint }
}

/**
 * write the line into the boot mask's `.boot-capacity` node (index.html, hidden until the first answer): the text, then
 * the outline badge when a queue is needed. The node keeps a fixed height (no layout jump). Returns false without a node.
 */
export function writeCapacityLine(el: HTMLElement | null, line: CapacityLine): boolean {
  if (!el) return false
  const key = `${line.kind}|${line.text}|${line.badge ?? ''}`
  if (el.dataset.capacityKey === key) return true
  el.dataset.capacityKey = key
  el.dataset.capacityHint = line.kind
  el.replaceChildren()
  const text = document.createElement('span')
  text.className = 'boot-capacity-text'
  text.textContent = line.text
  el.append(text)
  if (line.badge) {
    const b = document.createElement('span')
    b.className = 'boot-capacity-badge'
    b.dataset.capacityQueue = line.viewerQueue && line.sandboxQueue ? 'both' : line.viewerQueue ? 'viewer' : 'sandbox'
    b.textContent = line.badge
    el.append(b)
  }
  el.hidden = false
  return true
}

/** one public capacity request with a timeout (no token, no visitor identifier); rejects on timeout or a non-2xx answer */
export async function bootCapacityFetch(timeoutMs = BOOT_CAPACITY_TIMEOUT_MS, signal?: AbortSignal): Promise<CapacityView> {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeoutMs)
  const onAbort = (): void => ctl.abort()
  signal?.addEventListener('abort', onAbort, { once: true })
  try {
    const r = await fetch(CAPACITY_PATH, { cache: 'no-store', signal: ctl.signal, headers: { accept: 'application/json' }, credentials: 'omit' })
    if (!r.ok) throw new Error(`capacity ${r.status}`)
    const body = (await r.json()) as CapacityView
    if (!body || body.schema !== 'awr.capacity.v1') throw new Error('capacity schema')
    return body
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}
