// Control-message batch of rt.worker (M11-FR-095; M11 §6.4.14 point 3; AWR-10 §6.5 `ctrl`). Owner: M11.
// Control messages (JSON ops, decoded msgpack channels, worker notices) are queued and handed to the main thread at most
// once per frame together with a slot. While the main thread does not pull (hidden tab: rAF stopped) the events stay here,
// capped at EVENT_BUFFER_CAP events: beyond that the oldest event messages are dropped and a local gap is recorded
// (seq range and count) so the UI can backfill with GET /api/events?since= once visible again. Other control messages
// (results, status, conn) are still delivered on a slow timer. Low-frequency path: allocation is allowed here.
import type { CtrlMsg } from './decode'

export const EVENT_BUFFER_CAP = 8192

const isEvent = (m: CtrlMsg): boolean => m.op === 'event' || m.op === 'events'
const countOf = (m: CtrlMsg): number => (m.op === 'events' ? ((m.items as unknown[] | undefined)?.length ?? 0) : 1)
function seqRange(m: CtrlMsg): [number, number] {
  if (m.op === 'event') {
    const s = Number(m.seq) || 0
    return [s, s]
  }
  const items = (m.items as { seq?: number }[] | undefined) ?? []
  let lo = Number.POSITIVE_INFINITY
  let hi = 0
  for (const it of items) {
    const s = Number(it.seq) || 0
    if (s < lo) lo = s
    if (s > hi) hi = s
  }
  return [Number.isFinite(lo) ? lo : 0, hi]
}

export class CtrlQueue {
  private items: CtrlMsg[] = []
  /** events currently queued (items of `events` counted one by one) */
  events = 0
  /** events dropped since the last take() and their seq range */
  dropped = 0
  dropFrom = 0
  dropTo = 0
  /** total local gaps (drop episodes) */
  gaps = 0

  get length(): number {
    return this.items.length
  }

  push(m: CtrlMsg): void {
    this.items.push(m)
  }

  /** queue an event message; returns true when older events had to be dropped (local GAP) */
  pushEvent(m: CtrlMsg): boolean {
    this.items.push(m)
    this.events += countOf(m)
    if (this.events <= EVENT_BUFFER_CAP) return false
    let droppedNow = false
    while (this.events > EVENT_BUFFER_CAP) {
      const i = this.items.findIndex(isEvent)
      if (i < 0) break
      const old = this.items.splice(i, 1)[0]
      const n = countOf(old)
      const [lo, hi] = seqRange(old)
      this.events -= n
      if (this.dropped === 0) {
        this.dropFrom = lo
        this.gaps++
      }
      this.dropped += n
      this.dropTo = Math.max(this.dropTo, hi)
      droppedNow = true
    }
    return droppedNow
  }

  /** everything queued, with a `localGap` notice first when events were dropped */
  take(): CtrlMsg[] | null {
    if (!this.items.length && this.dropped === 0) return null
    const out = this.items
    if (this.dropped > 0) out.unshift({ op: 'localGap', fromSeq: this.dropFrom, toSeq: this.dropTo, dropped: this.dropped })
    this.items = []
    this.events = 0
    this.dropped = 0
    this.dropFrom = 0
    this.dropTo = 0
    return out
  }

  /** hidden page: hand out everything except events (which stay queued under the cap) */
  takeNonEvents(): CtrlMsg[] | null {
    if (!this.items.length) return null
    const keep: CtrlMsg[] = []
    const out: CtrlMsg[] = []
    for (const m of this.items) (isEvent(m) ? keep : out).push(m)
    this.items = keep
    return out.length ? out : null
  }
}
