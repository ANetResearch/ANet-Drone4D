// Prototype board helpers (BN-97; M27 §6.7.9): row order and number formatting for the /lab/board table. Ranked entries
// come first by non-dominated tier, then by EC rank and entry id; invalid entries (more than 20 % invalid runs: no rank,
// no tier) follow, then reference baselines in their own block (they take no rank and do not change the tiers of others). Values are shown with three decimals and the 95 % interval as [lo, hi].
import type { Board, BoardEntry, BoardMetric } from '@/net/bench'

export const METRICS = ['ec', 'eu', 'ao', 'ce'] as const

export function boardRows(board: Board | undefined, track: string | null, lane: string | null): BoardEntry[] {
  if (!board) return []
  const rows = board.entries.filter((e) => (track === null || e.track === track) && (lane === null || e.lane === lane))
  const key = (e: BoardEntry) => e.metrics.ec.rank ?? Number.MAX_SAFE_INTEGER
  const block = (e: BoardEntry) => (e.baseline ? 2 : e.status === 'invalid' ? 1 : 0)
  const tier = (e: BoardEntry) => e.dominance_tier ?? Number.MAX_SAFE_INTEGER
  return rows.toSorted((a, b) => block(a) - block(b) || tier(a) - tier(b) || key(a) - key(b)
    || (a.entry_id < b.entry_id ? -1 : a.entry_id > b.entry_id ? 1 : 0))
}

export function fmtTier(e: BoardEntry): string {
  return e.dominance_tier === null ? '—' : String(e.dominance_tier)
}

export function fmtValue(m: BoardMetric | undefined): string {
  return m?.value === null || m?.value === undefined ? '—' : m.value.toFixed(3)
}

export function fmtCi(m: BoardMetric | undefined): string {
  return m?.value === null || m?.value === undefined ? '' : `[${m.ci95[0].toFixed(3)}, ${m.ci95[1].toFixed(3)}]`
}

/** rank text: tied entries share the rank of the first member of their tie group, marked with "=" */
export function fmtRank(rows: readonly BoardEntry[], e: BoardEntry, k: (typeof METRICS)[number]): string {
  const m = e.metrics[k]
  if (m.rank === undefined) return '—'
  const tied = rows.some((o) => o !== e && !o.baseline && o.metrics[k].tie_group === m.tie_group)
  return tied ? `=${m.rank}` : String(m.rank)
}

export function fmtNum(v: number | null | undefined, digits = 3): string {
  return v === null || v === undefined ? '—' : v.toFixed(digits)
}
