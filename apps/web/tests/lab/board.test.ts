import { createElement as h, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it } from 'vitest'
import { setLocale } from '@/app/i18n'
import en from '@/app/i18n/en.json'
import zhCN from '@/app/i18n/zh-CN.json'
import type { Board, BoardEntry, BoardIndex, BoardMetric, Submission } from '@/net/bench'
import { boardRows, fmtCi, fmtRank, fmtTier, fmtValue } from '@/ui/lab/board'
import { LabBoardPage } from '@/ui/lab/LabBoardPage'
import { LabSubmissionsPage } from '@/ui/lab/LabSubmissionsPage'

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
  it('never mixes tracks or lanes in one table', () => {
    const x = { ...e('x', 1, 1, 0), lane: 'other' }
    const b = board([e('a', 1, 1, 0), e('b', 1, 1, 0, false, 'u'), x])
    expect(boardRows(b, 't', 'l').map((r) => r.entry_id)).toEqual(['a'])
    expect(boardRows(b, 'u', 'l').map((r) => r.entry_id)).toEqual(['b'])
    expect(boardRows(b, 't', 'other').map((r) => r.entry_id)).toEqual(['x'])
  })
})

// D3-AC-57（ADR-191）：weights = null 时页面不出现合成主得分（反向断言）。接口若误带合成字段（下方注入的 0.777 与 0.888），页面也不得显示
const COMPOSITE = 0.777
const COMPOSITE_CI = 0.888
const TRACK = { track: 'std3-l1', fleet_ref: '3x-x500', obs_level: 'L1', physics: 'realistic', mode: 'exogenous', agent_class: 'python', appearance: 'mesh' }
const LANE = { lane: 'rendezvous_handoff', kind: 'event_type', event_type: 'rendezvous_handoff', test_events: ['crop-handoff-001'], seed_set_commit: 'c'.repeat(64) }

function withComposite<T extends object>(x: T): T {
  return { ...x, composite: COMPOSITE, composite_ci95: [COMPOSITE_CI, COMPOSITE_CI], s: COMPOSITE } as T
}

function client(): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['lab', 'session'], { principal: 'alice', role: 'researcher', projects: ['d3'] })
  const idx: BoardIndex = { current: 'proto-0', versions: [{ board_version: 'proto-0', current: true, score_version: 'd3-score-0', weights: null,
    tracks: [TRACK], lanes: [LANE], n_entries: 2 }] }
  qc.setQueryData(['lab', 'board'], idx)
  const entries = [{ ...e('team-a/v1', 1, 1, 0, false, 'std3-l1'), lane: LANE.lane }, { ...e('baseline/x', 1, undefined, undefined, true, 'std3-l1'), lane: LANE.lane }]
  qc.setQueryData(['lab', 'leaderboard', 'proto-0'], withComposite({ ...board(entries.map(withComposite)), board_version: 'proto-0', tracks: [TRACK], lanes: [LANE] }))
  const sub: Submission = { schema: 'drone4d.bench.submission.v1', submission_id: 'sub-abc', track: 'std3-l1', image_digest: `sha256:${'a'.repeat(64)}`,
    entry: { cmd: ['python', '-m', 'agent'] }, state: 'DONE', visibility: 'board', position: 0,
    result: { lanes: [withComposite({ lane: LANE.lane, metrics: { ec: m(0.5), eu: m(0.1), ao: m(0.3), ce: m(0.2) }, runs: { total: 2, valid: 2, invalid: 0, platform_rerun: 1 },
      status: 'valid', composite: null })] } }
  qc.setQueryData(['lab', 'submissions'], { items: [sub] })
  return qc
}

const render = (page: () => ReactElement | null) => renderToStaticMarkup(h(QueryClientProvider, { client: client() }, h(page)))
const headers = (html: string) => [...html.matchAll(/<th[^>]*>(.*?)<\/th>/g)].map((x) => x[1].replace(/<[^>]+>/g, ''))

describe('weights = null: no composite score on the lab pages (D3-AC-57)', () => {
  afterEach(() => setLocale('zh-CN'))

  for (const loc of ['zh-CN', 'en'] as const) {
    it(`board page (${loc}) shows only the four metrics, ranks and tiers`, () => {
      setLocale(loc)
      const html = render(LabBoardPage)
      expect(html).toContain('data-figure="lab-board-table"')
      expect(html).toContain('team-a/v1')
      expect(html).not.toContain(String(COMPOSITE))
      expect(html).not.toContain(String(COMPOSITE_CI))
      const note = /<p class="mt-2[^"]*">(.*?)<\/p>/.exec(html)?.[1] ?? ''
      expect(note).toMatch(/不计算合成分|No composite score is computed/)          // 说明文字声明不计算合成分
      expect(html.replace(note, '')).not.toMatch(/合成|主得分|composite/i)
      const cols = headers(html)
      expect(cols).toHaveLength(3 + 4 + 4)                                   // 层级、条目、团队 + 四项指标 + 四项名次，无合成列
      for (const c of cols) expect(c).not.toMatch(/合成|主得分|composite|score/i)
    })
    it(`submissions page (${loc}) shows per-lane metrics without a composite`, () => {
      setLocale(loc)
      const html = render(LabSubmissionsPage)
      expect(html).toContain('sub-abc')
      expect(html).not.toContain(String(COMPOSITE))
      expect(html).not.toContain(String(COMPOSITE_CI))
      for (const c of headers(html)) expect(c).not.toMatch(/合成|主得分|composite|score/i)
    })
  }

  it('lab dictionaries carry no composite label; the board note states that none is computed', () => {
    for (const d of [zhCN, en] as Record<string, string>[]) {
      const keys = Object.keys(d).filter((k) => k.startsWith('lab.board.') || k.startsWith('lab.sub.'))
      expect(keys.length).toBeGreaterThan(20)
      expect(keys.filter((k) => /composite|weight|score/i.test(k))).toEqual([])
      for (const k of keys.filter((x) => x !== 'lab.board.note')) expect(d[k]).not.toMatch(/合成|主得分|composite/i)
    }
    expect(zhCN['lab.board.note' as keyof typeof zhCN]).toContain('不计算合成分')
  })
})
