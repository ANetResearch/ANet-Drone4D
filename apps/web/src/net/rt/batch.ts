// Batch-call helpers (M11-FR-061; AWR-17 §7.5 item 5; AWR-14 §6.11 item 3). Owner: M11. The summary result of
// fleet/cmd/{op} carries data{accepted_n, rejected_n, rejected_by_code, accepted, rejected}; progress and the
// fleet.batch.progress event carry data.counts; the final result carries data.counts as well.
import type { BatchCounts, BatchSummary, CallResult, Progress, RtEvent } from './types'

const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)

/** the summary of the first result of a batch call, or null when the result is not a batch summary */
export function batchSummaryOf(r: CallResult): BatchSummary | null {
  const d = r.data
  if (!d || typeof d.accepted_n !== 'number') return null
  return {
    accepted_n: num(d.accepted_n),
    rejected_n: num(d.rejected_n),
    rejected_by_code: (d.rejected_by_code ?? {}) as Record<string, number>,
    accepted: Array.isArray(d.accepted) ? (d.accepted as unknown[]).map(String) : [],
    rejected: Array.isArray(d.rejected) ? (d.rejected as [unknown, unknown][]).map(([id, code]) => [String(id), num(code)] as [string, number]) : [],
  }
}

/** counts from a progress message, a final batch result or a fleet.batch.progress event; null when absent */
export function batchCountsOf(x: Progress | CallResult | RtEvent): BatchCounts | null {
  const c = (x.data as { counts?: Record<string, unknown> } | undefined)?.counts
  if (!c || typeof c !== 'object') return null
  return { accepted: num(c.accepted), running: num(c.running), succeeded: num(c.succeeded), failed: num(c.failed), canceled: num(c.canceled),
    rejected: num(c.rejected) }
}

/** terminal status (AWR-17 §7.2): succeeded, failed, canceled, rejected, timeout */
export function isFinalResult(r: CallResult): boolean {
  return r.final === true || r.status === 'succeeded' || r.status === 'failed' || r.status === 'canceled' || r.status === 'rejected' || r.status === 'timeout'
}
