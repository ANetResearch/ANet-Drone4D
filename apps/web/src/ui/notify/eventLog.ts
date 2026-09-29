// Event log of the Events tab (M15-FR-025, FR-033; AWR-14 §4.5; ADR-028): a fixed ring of LIMITS.eventLogCap (5000)
// entries, the oldest dropped when full. Slots hold the event references handed over by the event bridge plus the
// columns the table sorts and filters on (wall time, level, severity rank); display strings are computed lazily for the
// visible rows only and cached per slot. The store only carries {version, len, dropped} and is written by the bridge
// flush (<= 4 Hz).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { LIMITS } from '@/lib/tokens/input.gen'
import type { RtEvent } from '@/net/rt'
import { describeEvent, eventSeverity, type Sev, type SevInfo } from './severity'

export interface EventLogState { version: number; len: number; dropped: number }
export const eventLogStore = createAwrStore<EventLogState>('ui.eventLog', () => ({ version: 0, len: 0, dropped: 0 }))
export const useEventLog = <T,>(sel: (s: EventLogState) => T): T => useStore(eventLogStore, sel)

export class EventRing {
  readonly cap: number
  readonly ev: (RtEvent | null)[]
  readonly tWallMs: Float64Array
  readonly sev: Uint8Array
  readonly rank: Uint8Array
  readonly text: (string | null)[]
  head = 0
  len = 0
  dropped = 0
  private readonly si: SevInfo = { sev: 'info', rank: 0 }
  constructor(cap: number) {
    this.cap = cap
    this.ev = new Array<RtEvent | null>(cap).fill(null)
    this.text = new Array<string | null>(cap).fill(null)
    this.tWallMs = new Float64Array(cap)
    this.sev = new Uint8Array(cap)
    this.rank = new Uint8Array(cap)
  }
  push(e: RtEvent, wallMs: number): void {
    const i = this.head
    this.ev[i] = e
    this.text[i] = null
    this.tWallMs[i] = wallMs
    const s = eventSeverity(e, this.si)
    this.sev[i] = s.sev === 'critical' ? 2 : s.sev === 'warning' ? 1 : 0
    this.rank[i] = Math.min(255, Math.max(0, s.rank))
    this.head = (i + 1) % this.cap
    if (this.len < this.cap) this.len++
    else this.dropped++
  }
  /** ring slot of the k-th newest entry (0 = newest) */
  slotOfNewest(k: number): number {
    return (this.head - 1 - k + 2 * this.cap) % this.cap
  }
  get(slot: number): RtEvent | null {
    return this.ev[slot]
  }
  textOf(slot: number): string {
    let s = this.text[slot]
    if (s === null) {
      const e = this.ev[slot]
      s = e ? describeEvent(e) : ''
      this.text[slot] = s
    }
    return s
  }
  sevOf(slot: number): Sev {
    const v = this.sev[slot]
    return v === 2 ? 'critical' : v === 1 ? 'warning' : 'info'
  }
  clear(): void {
    this.ev.fill(null)
    this.text.fill(null)
    this.head = 0
    this.len = 0
  }
}

export const eventLog = new EventRing(LIMITS.eventLogCap)

/** publish the ring after a bridge flush */
export function commitEventLog(): void {
  const s = eventLogStore.getState()
  eventLogStore.setState({ version: s.version + 1, len: eventLog.len, dropped: eventLog.dropped })
}
