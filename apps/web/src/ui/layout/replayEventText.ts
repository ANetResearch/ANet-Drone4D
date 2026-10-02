// Replay hover text of the Timeline (M12 §8.4; 17 R68): in replay the markers come from the segment's .evx index, which
// carries no event body. When the pointer rests on a marker for 150 ms the track area asks R68 for the events of that
// marker's pixel column (at most 20) and caches their text by mseq (the in-segment write ordinal shared by .evx and
// R68); the cache belongs to one run and segment and is dropped when either changes. Live mode reads the event log
// instead (TimelineTrackArea.eventTextOfSeq). Failures (409 463 REPLAY_NOT_OPEN, 503 213) leave the class label alone
// and are not retried for the same window.
import { apiGet } from '@/net/api'
import type { RtEvent } from '@/net/rt'
import { describeEvent } from '@/ui/notify/severity'

/** hover dwell before the request (M12 §8.4) */
export const REPLAY_HOVER_DWELL_MS = 150
/** events per request (M12 §8.4) */
export const REPLAY_HOVER_LIMIT = 20
const CAP = 2000

interface R68Item { mseq?: number | null; type?: string; uav?: unknown; data?: unknown }

let owner = ''
const text = new Map<number, string>()
const asked = new Set<string>()

function own(run: string, seg: number): void {
  const k = `${run}/${seg}`
  if (k === owner) return
  owner = k
  text.clear()
  asked.clear()
}

/** cached text of the replay event with this mseq in run/seg; '' when not fetched (yet) */
export function replayEventText(run: string | null, seg: number, mseq: number): string {
  if (!run || owner !== `${run}/${seg}`) return ''
  return text.get(mseq) ?? ''
}

/** R68 path for one column window (exported for tests) */
export function r68Path(run: string, seg: number, fromNs: number, toNs: number): string {
  const q = new URLSearchParams({ seg: String(seg), from_ns: String(Math.max(0, Math.floor(fromNs))), to_ns: String(Math.ceil(toNs)), limit: String(REPLAY_HOVER_LIMIT) })
  return `/api/runs/${encodeURIComponent(run)}/events?${q.toString()}`
}

/** store the text of R68 items (exported for tests) */
export function ingestReplayEvents(run: string, seg: number, items: readonly R68Item[]): number {
  own(run, seg)
  let n = 0
  for (const it of items) {
    if (typeof it?.mseq !== 'number' || typeof it.type !== 'string') continue
    const e: Pick<RtEvent, 'type' | 'data' | 'uav'> = {
      type: it.type, uav: typeof it.uav === 'string' ? it.uav : null,
      data: it.data && typeof it.data === 'object' ? (it.data as RtEvent['data']) : {},
    }
    if (text.size >= CAP) text.delete(text.keys().next().value as number)
    text.set(it.mseq >>> 0, describeEvent(e))
    n++
  }
  return n
}

/** fetch the events of [fromS, toS] (sim seconds) once; resolves true when new text arrived */
export async function fetchReplayEvents(run: string, seg: number, fromS: number, toS: number): Promise<boolean> {
  own(run, seg)
  const path = r68Path(run, seg, fromS * 1e9 - 1, toS * 1e9 + 1)
  if (asked.has(path)) return false
  asked.add(path)
  try {
    const r = await apiGet<{ items?: R68Item[] }>(path)
    if (owner !== `${run}/${seg}`) return false
    return ingestReplayEvents(run, seg, Array.isArray(r?.items) ? r.items : []) > 0
  } catch {
    return false
  }
}
