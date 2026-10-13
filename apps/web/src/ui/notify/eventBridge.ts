// Event bridge (M15-FR-036, §6.6.2; ADR-028; D1-AC-27): RtClient.onEvents delivers at most one batch per frame; the
// bridge only copies the event references into a preallocated ring (LIMITS.bridgeRingCap = 8192, the oldest are
// overwritten and counted as dropped) and every INPUT.bridgeFlushMs (250 ms = 4 Hz) folds the ring into the event log,
// the alarms, the toast merger, the batch command tracker and the query invalidation table, with one store write each.
// Flush time and drops go to window.__ux.bridge in dev and test builds (not to __perf). Test builds also expose
// window.__uxInject.events(batch) so storm specs can inject events without a backend.
// D2.2-W1: events that describe the simulation of a world (vehicles, targets, perception, tasks, nests, missions, the run)
// pass only while the realtime source simulates the world the view shows (stores/worldScope); system, job, world-build and
// sandbox-session events always pass.
import { navigate } from '@/app/router/router'
import { queryClient } from '@/app/query/client'
import { applyInvalidation, planInvalidation } from '@/app/query/eventInvalidation'
import { INPUT, LIMITS } from '@/lib/tokens/input.gen'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import type { RtClient, RtEvent } from '@/net/rt'
import { selection } from '@/stores/selection'
import { UX } from '@/ui/testing/uxProbe'
import { noteUiWork } from './afterLayout'
import { commitAlarms, consumeAlarm, type AlarmItem } from './alarms'
import { commitEventLog, eventLog } from './eventLog'
import { skipAlarmToast, toastAlarm, toastInfoEvent } from './toastMerger'
import { freezableInterval } from '@/lib/powerFreeze'
import { worldLive } from '@/stores/worldScope'

type BatchListener = (e: RtEvent) => void
const batchListeners = new Set<BatchListener>()
/** per-event hook of other UI modules (the batch command tracker listens for fleet.batch.progress) */
export function onBridgeEvent(cb: BatchListener): () => void {
  batchListeners.add(cb)
  return () => {
    batchListeners.delete(cb)
  }
}

class BridgeRing {
  readonly ev: (RtEvent | null)[]
  readonly wall: Float64Array
  head = 0
  len = 0
  dropped = 0
  constructor(readonly cap: number) {
    this.ev = new Array<RtEvent | null>(cap).fill(null)
    this.wall = new Float64Array(cap)
  }
  write(e: RtEvent, wallMs: number): void {
    const i = (this.head + this.len) % this.cap
    this.ev[i] = e
    this.wall[i] = wallMs
    if (this.len < this.cap) this.len++
    else {
      this.head = (this.head + 1) % this.cap
      this.dropped++
    }
  }
}

const ring = new BridgeRing(LIMITS.bridgeRingCap)
const touched = new Set<AlarmItem>()
const batchView: RtEvent[] = []

/** event types that do not describe a world's simulation: they pass the world gate */
const WORLD_FREE = /^(sys|job|sandbox|session|world|proc|recorder|rec|lease|power|identity)\./
/** the event may be shown with the current world scope (exported for tests) */
export function eventInScope(e: Pick<RtEvent, 'type'>): boolean {
  return worldLive() || WORLD_FREE.test(e.type)
}

/** copy one batch (called from the RtClient callback; no allocation besides the ring slots) */
export function pushEvents(batch: readonly RtEvent[]): void {
  const now = Date.now()
  const live = worldLive()
  for (let i = 0; i < batch.length; i++) if (live || WORLD_FREE.test(batch[i].type)) ring.write(batch[i], now)
}

/** events not yet flushed belong to the previous world after a world scope reset: drop them (ui/shell/worldReset.ts) */
export function dropPendingEvents(): void {
  for (let k = 0; k < ring.len; k++) ring.ev[(ring.head + k) % ring.cap] = null
  ring.head = 0
  ring.len = 0
}

let rosterSeen = -1
let rtRef: RtClient | null = null

/** fold the ring into the UI stores (exported for tests; normally driven by the 4 Hz timer) */
export function flushEvents(): void {
  const rt = rtRef
  // roster changes prune selections of vehicles that no longer exist (epoch restart, vehicle removed)
  if (rt && rt.roster.version !== rosterSeen) {
    rosterSeen = rt.roster.version
    if (rt.roster.size > 0) selection.prune((id) => rt.roster.agentNoOf(id) >= 0)
  }
  if (ring.len === 0) return
  const t0 = performance.now()
  batchView.length = 0
  for (let k = 0; k < ring.len; k++) {
    const i = (ring.head + k) % ring.cap
    const e = ring.ev[i]
    if (!e) continue
    ring.ev[i] = null
    batchView.push(e)
    eventLog.push(e, ring.wall[i])
    const a = consumeAlarm(e, ring.wall[i])
    if (a && !skipAlarmToast(a)) touched.add(a)
    else if (!a) toastInfoEvent(e)
    for (const l of batchListeners) l(e)
  }
  ring.head = 0
  ring.len = 0
  for (const a of touched) toastAlarm(a)
  touched.clear()
  applyInvalidation(queryClient, planInvalidation(batchView), (w) => navigate(`/world/${w}`))
  batchView.length = 0
  commitEventLog()
  commitAlarms()
  noteUiWork() // a toast write waits for a frame without this flush (ADR-081)
  if (TEST_SWITCHES) {
    const ms = performance.now() - t0
    UX.bridge.flushes++
    if (ms > 2) UX.bridge.over2ms++
    if (ms > UX.bridge.maxMs) UX.bridge.maxMs = ms
    UX.bridge.dropped = ring.dropped
  }
}

let stopTimer: (() => void) | null = null
/** attach to the page RtClient (RtProvider effect); returns the detach function */
export function installEventBridge(rt: RtClient): () => void {
  rtRef = rt
  const off = rt.onEvents(pushEvents)
  // not while the power-save mode froze the page (D2-AC-44: the link is closed there; buffered events flush on resume)
  if (!stopTimer) stopTimer = freezableInterval(flushEvents, INPUT.bridgeFlushMs)
  if (TEST_SWITCHES && typeof window !== 'undefined') {
    const w = window as unknown as { __uxInject?: Record<string, unknown> }
    w.__uxInject = { ...w.__uxInject, events: (b: RtEvent[]) => pushEvents(b), flush: () => flushEvents() }
  }
  return () => {
    off()
    stopTimer?.()
    stopTimer = null
    rtRef = null
  }
}

export const bridgeStats = { dropped: (): number => ring.dropped, pending: (): number => ring.len }
