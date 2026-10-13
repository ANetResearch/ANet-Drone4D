// Capacity store (M17-FR-078, AC-032; AWR-04 §15.5.3, §15.6.5; ADR-119, ADR-120). Owner: M17.
// The public capacity endpoint SB-71 (`awr.capacity.v1`): live viewer seats, sandbox slots, the queue and the expected
// wait, the build of the server and the power thresholds.
//   * boot: the entry module calls startBootCapacity() in its first microtask; the request has a 1.5 s timeout and repeats
//     every 5 s while the boot mask is visible; stopBootCapacity() at reveal. Never gates the reveal (D2-AC-41);
//   * after the reveal the store polls every 10 s only while a visible consumer holds a lease (landing page, queue card,
//     power overlay, "open my sandbox" sheet): acquireCapacityPolling(owner) returns the release function;
//   * `updated` turns true when build.release differs from the release baked into this bundle (`__AWR_BUILD__`, M00 vite
//     plugin); local builds (channel `local`) never compare (AWR-04 §15.4.2 item 3).
// The request carries no token and no principal (privacy row of §15.5.3). Text formatting is M15's (i18n `capacity.*`);
// this store only offers the numeric helpers etaBucket() and queueNeeded(). Actions are plain functions (STORE-01).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { getCapacity } from '@/net/sandbox/client'
import type { BuildInfo, CapacityView } from '@/net/sandbox/client'

export type CapacityPhase = 'idle' | 'loading' | 'ok' | 'unavailable'
export interface CapacityState {
  phase: CapacityPhase
  data?: CapacityView
  /** wall-clock ms of the last successful response */
  fetchedAtMs?: number
  /** the server runs a different release than this bundle: show "site updated · reload" (never reload automatically) */
  updated: boolean
  /** consecutive failures since the last success */
  failures: number
}

export const BOOT_TIMEOUT_MS = 1500
export const BOOT_REFRESH_MS = 5000
export const POLL_MS = 10_000

export const capacityStore = createAwrStore<CapacityState>('capacity', () => ({ phase: 'idle', updated: false, failures: 0 }))

export function useCapacity<T>(sel: (s: CapacityState) => T): T {
  return useStore(capacityStore, sel)
}

// ------------------------------------------------------------ environment (injectable for tests)
declare const __AWR_BUILD__: BuildInfo | undefined

function bundledBuild(): BuildInfo | null {
  try {
    if (typeof __AWR_BUILD__ !== 'undefined' && __AWR_BUILD__) return __AWR_BUILD__
  } catch {
    // not defined by the bundler (tests, older builds)
  }
  const g = (globalThis as { __AWR_BUILD__?: BuildInfo }).__AWR_BUILD__
  return g ?? null
}

interface Deps {
  fetch: (signal: AbortSignal) => Promise<CapacityView>
  now: () => number
  build: () => BuildInfo | null
  setTimer: (fn: () => void, ms: number) => unknown
  clearTimer: (h: unknown) => void
}

function defaultDeps(): Deps {
  return {
    fetch: (signal) => getCapacity(signal), now: () => Date.now(), build: bundledBuild,
    setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  }
}

let deps: Deps = defaultDeps()
let bootTimer: unknown = null
let pollTimer: unknown = null
let inflight: Promise<CapacityState> | null = null
const owners = new Set<string>()

/** test hook: replace the fetcher, clock, bundled build and timers; resets the store and the timers */
export function configureCapacity(over: Partial<Deps>): void {
  stopBootCapacity()
  stopPolling()
  owners.clear()
  inflight = null
  deps = { ...defaultDeps(), ...over }
  capacityStore.setState({ phase: 'idle', updated: false, failures: 0, data: undefined, fetchedAtMs: undefined }, true)
}

/**
 * production hand-over of the request the early boot script already has in flight (d2-WP-12c-to-M17 item 1, D2-INT): the
 * store's next fetch returns that promise (no second request, so the 5 s spacing of the mask period holds); later fetches
 * go out normally. Unlike configureCapacity() the store state, the timers and the leases are kept.
 */
export function adoptBootCapacity(p: Promise<CapacityView> | null): void {
  if (!p) return
  let first: Promise<CapacityView> | null = p
  const base = deps.fetch
  deps = {
    ...deps,
    fetch: (signal) => {
      const q = first
      first = null
      return q ?? base(signal)
    },
  }
}

// ------------------------------------------------------------ fetching
function isUpdated(server: BuildInfo | undefined): boolean {
  const mine = deps.build()
  if (!server || !mine || mine.channel === 'local' || server.channel === 'local') return false
  return !!server.release && !!mine.release && server.release !== mine.release
}

/** one request with a timeout; concurrent callers share the request in flight */
export function refreshCapacity(timeoutMs = BOOT_TIMEOUT_MS): Promise<CapacityState> {
  if (inflight) return inflight
  const ctl = new AbortController()
  let fail: ((e: Error) => void) | null = null
  const expired = new Promise<never>((_r, rej) => { fail = rej })
  const timer = deps.setTimer(() => {
    ctl.abort()
    fail?.(new Error('timeout'))
  }, timeoutMs)
  const cur = capacityStore.getState()
  if (cur.phase === 'idle') capacityStore.setState({ phase: 'loading' })
  const p = (async (): Promise<CapacityState> => {
    try {
      const data = await Promise.race([deps.fetch(ctl.signal), expired])
      if (!data || data.schema !== 'awr.capacity.v1') throw new Error('schema')
      const updated = capacityStore.getState().updated || isUpdated(data.build)
      capacityStore.setState({ phase: 'ok', data, fetchedAtMs: deps.now(), updated, failures: 0 })
    } catch {
      const s = capacityStore.getState()
      capacityStore.setState({ phase: s.data ? s.phase : 'unavailable', failures: s.failures + 1 })
    } finally {
      deps.clearTimer(timer)
      inflight = null
    }
    return capacityStore.getState()
  })()
  inflight = p
  return p
}

// ------------------------------------------------------------ boot (mask visible)
/** entry module, first microtask: request now (1.5 s timeout) and every 5 s until stopBootCapacity(); never gates the reveal */
export function startBootCapacity(): () => void {
  if (bootTimer !== null) return stopBootCapacity
  const tick = (): void => {
    void refreshCapacity(BOOT_TIMEOUT_MS)
    bootTimer = deps.setTimer(tick, BOOT_REFRESH_MS)
  }
  bootTimer = 0
  tick()
  return stopBootCapacity
}

/** the reveal hands over to the post-reveal rule (poll only while a visible consumer holds a lease) */
export function stopBootCapacity(): void {
  if (bootTimer !== null && bootTimer !== 0) deps.clearTimer(bootTimer)
  bootTimer = null
}

// ------------------------------------------------------------ after the reveal: leases of visible consumers
function stopPolling(): void {
  if (pollTimer !== null) deps.clearTimer(pollTimer)
  pollTimer = null
}

function schedulePoll(): void {
  stopPolling()
  if (owners.size === 0) return
  pollTimer = deps.setTimer(() => {
    pollTimer = null
    void refreshCapacity(BOOT_TIMEOUT_MS).finally(schedulePoll)
  }, POLL_MS)
}

/** a visible consumer (landing, queue card, power overlay, start sheet) asks for 10 s updates; returns the release */
export function acquireCapacityPolling(owner: string): () => void {
  const first = owners.size === 0
  owners.add(owner)
  if (first) {
    const s = capacityStore.getState()
    const stale = !s.fetchedAtMs || deps.now() - s.fetchedAtMs >= POLL_MS
    if (stale && bootTimer === null) void refreshCapacity(BOOT_TIMEOUT_MS)
    schedulePoll()
  }
  let released = false
  return () => {
    if (released) return
    released = true
    owners.delete(owner)
    if (owners.size === 0) stopPolling()
  }
}

export function capacityPollingOwners(): string[] {
  return [...owners]
}

// ------------------------------------------------------------ numeric helpers for the UI texts (AWR-04 §15.6.5)
export type EtaBucket = { kind: 'unknown' } | { kind: 'lt1min' } | { kind: 'min'; n: number } | { kind: 'h'; n: number }

/** `< 1 min`, `about n min`, `about n h`; null is "cannot estimate" */
export function etaBucket(etaS: number | null | undefined): EtaBucket {
  if (etaS === null || etaS === undefined || !Number.isFinite(etaS) || etaS < 0) return { kind: 'unknown' }
  if (etaS < 60) return { kind: 'lt1min' }
  if (etaS < 3600) return { kind: 'min', n: Math.max(1, Math.round(etaS / 60)) }
  return { kind: 'h', n: Math.max(1, Math.round(etaS / 3600)) }
}

/** which side would make a new visitor queue (boot text and the "needs queue" badge) */
export function queueNeeded(d: CapacityView | undefined): { viewer: boolean; sandbox: boolean } {
  if (!d) return { viewer: false, sandbox: false }
  return { viewer: d.viewers.state === 'full', sandbox: d.sandbox.state === 'full' || d.sandbox.state === 'queue' }
}
