import { describe, expect, it } from 'vitest'
import type { Board, BoardEntry, BoardMetric } from '@/net/bench'
import { boardRows, fmtCi, fmtRank, fmtTier, fmtValue } from '@/ui/lab/board'

const m = (value: number | null, rank?: number, tie_group?: number): BoardMetric => ({ value, ci95: [0.1, 0.2], ...(rank ? { rank, tie_group } : {}) })
const e = (id: string, tier: number | null, ecRank: number | undefined, tie: number | undefined, baseline = false, track = 't'): BoardEntry => ({
  entry_id: id, team: 'x', display_name: id, track, lane: 'l', dominance_tier: tier, first_listed_unix_ns: 0, baseline,
  metrics: { ec: m(0.5, ecRank, tie), eu: m(0.1), ao: m(null), ce: m(0.2) },
})
const board = (entries: BoardEntry[]): Board => ({ schema: 'drone4d.bench.board.v1', board_version: 'p', tracks: [], lanes: [], seed_set_commit: '',
  evaluator_major: 0, platform_digest: '', entries })

describe('prototype board rows', () => {
  it('orders ranked entries by tier and EC rank, baselines last, filters track', () => {
    const rows = boardRows(board([e('base', 1, undefined, undefined, true), e('b', 2, 1, 0), e('c', 1, 3, 1), e('a', 1, 2, 0), e('z', 1, 1, 0, false, 'u')]), 't', null)
    expect(rows.map((r) => r.entry_id)).toEqual(['a', 'c', 'b', 'base'])
  })
  it('puts invalid entries after ranked ones and before baselines, without a tier', () => {
    const bad = { ...e('bad', null, undefined, undefined), status: 'invalid' as const }
    const rows = boardRows(board([e('base', 1, undefined, undefined, true), bad, e('b', 2, 2, 1), e('a', 1, 1, 0)]), null, null)
    expect(rows.map((r) => r.entry_id)).toEqual(['a', 'b', 'bad', 'base'])
    expect(fmtTier(bad)).toBe('—')
    expect(fmtTier(rows[0])).toBe('1')
    expect(fmtRank(rows, bad, 'ec')).toBe('—')
  })
  it('marks tie groups and formats values', () => {
    const rows = [e('a', 1, 1, 0), e('b', 1, 1, 0), e('c', 1, 3, 1), e('base', 1, undefined, undefined, true)]
    expect(fmtRank(rows, rows[0], 'ec')).toBe('=1')
    expect(fmtRank(rows, rows[2], 'ec')).toBe('3')
    expect(fmtRank(rows, rows[3], 'ec')).toBe('—')
    expect(fmtValue(rows[0].metrics.ao)).toBe('—')
    expect(fmtValue(rows[0].metrics.ec)).toBe('0.500')
    expect(fmtCi(rows[0].metrics.ec)).toBe('[0.100, 0.200]')
    expect(fmtCi(rows[0].metrics.ao)).toBe('')
  })
})
