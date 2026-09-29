// M05-AC-016: GPU eviction plan (port of openlidarviewer tests/evictionPolicy.test.ts to the typed-array API with B_ref).
// A resident visible node is not dropped for a marginal overshoot; cheap nodes go first (outside the frustum, then in
// view but not selected, then the dwell-protected ones); within a class deeper first, then farther; release to
// 1.15 B_ref; hysteresis is a preference, never a veto; the plan does not depend on the candidate order.
import { describe, expect, it } from 'vitest'
import { CLASS_DWELL, CLASS_OUTSIDE, CLASS_VIEW, evictionClass, newEvictionCandidates, planEviction, type EvictionCandidates } from '@/engine/pointcloud/core/EvictionPolicy'
import { PC } from '@/engine/pointcloud/params'

interface C { node: number; pts?: number; level?: number; dist?: number; outside?: boolean; since?: number }
const SETTLED = 10 * PC.dwellMs

function cands(list: C[], force = false, need = 0): EvictionCandidates {
  const c = newEvictionCandidates(list.length)
  list.forEach((x, k) => {
    c.node[k] = x.node
    c.pts[k] = x.pts ?? 1000
    c.level[k] = x.level ?? 3
    c.dist[k] = x.dist ?? 10
    c.outside[k] = x.outside ? 1 : 0
    c.residentSince[k] = x.since ?? 0
  })
  c.n = list.length
  c.force = force
  c.needPts = need
  return c
}
const out = new Int32Array(1024)
const plan = (c: EvictionCandidates, resident: number, Bref: number, now = SETTLED): number[] => Array.from(out.subarray(0, planEviction(c, resident, Bref, now, out)))
const total = (list: C[]): number => list.reduce((a, x) => a + (x.pts ?? 1000), 0)

describe('a node oscillating at the budget boundary is not churned', () => {
  it('plans nothing while the overshoot stays inside the 1.5 B_ref margin', () => {
    for (const ratio of [1.0, 1.05, 1.2, 1.4, 1.49, 1.5]) expect(plan(cands([{ node: 1 }, { node: 2 }, { node: 3 }]), ratio * 100_000, 100_000)).toEqual([])
  })
  it('stops inside the band at 1.15 B_ref instead of cutting to the budget', () => {
    const list = Array.from({ length: 40 }, (_, i) => ({ node: i + 1, pts: 5000 }))
    const resident = 200_000
    const e = plan(cands(list), resident, 100_000)
    const released = e.length * 5000
    expect(resident - released).toBeLessThanOrEqual(115_000)
    expect(resident - released).toBeGreaterThanOrEqual(115_000 - 5000)
  })
})

describe('cheap-to-lose nodes are evicted before resident visible ones', () => {
  it('takes out-of-view before in-view and in-view before dwell-protected', () => {
    const list = [{ node: 1, since: SETTLED - 10 }, { node: 2 }, { node: 3, outside: true }]
    expect(plan(cands(list), 400_000, 100_000)).toEqual([3, 2, 1])
    const c = cands(list)
    expect([evictionClass(c, 0, SETTLED), evictionClass(c, 1, SETTLED), evictionClass(c, 2, SETTLED)]).toEqual([CLASS_DWELL, CLASS_VIEW, CLASS_OUTSIDE])
  })
  it('within one class takes the deeper node, then the farther one, then the lower index', () => {
    const list = [{ node: 5, level: 2, dist: 100 }, { node: 6, level: 4, dist: 10 }, { node: 7, level: 4, dist: 50 }, { node: 4, level: 2, dist: 100 }]
    expect(plan(cands(list), 400_000, 100_000)).toEqual([7, 6, 4, 5])
  })
  it('a future or non-finite residency time reads as just arrived (protected)', () => {
    const c = cands([{ node: 1, since: SETTLED + 5000 }, { node: 2, since: Number.NaN }])
    expect(evictionClass(c, 0, SETTLED)).toBe(CLASS_DWELL)
    expect(evictionClass(c, 1, SETTLED)).toBe(CLASS_DWELL)
  })
})

describe('hysteresis is a preference, never a veto', () => {
  it('reaches the release target even when every candidate is dwell-protected', () => {
    const list = Array.from({ length: 20 }, (_, i) => ({ node: i + 1, pts: 10_000, since: SETTLED }))
    const e = plan(cands(list), 250_000, 100_000)
    expect(250_000 - e.length * 10_000).toBeLessThanOrEqual(115_000)
  })
  it('keeps the resident set bounded across a long run of pressured ticks', () => {
    let list = Array.from({ length: 30 }, (_, i) => ({ node: i + 1, pts: 8000, since: 0 }))
    for (let tick = 0; tick < 50; tick++) {
      const resident = total(list)
      const e = new Set(plan(cands(list), resident, 100_000, SETTLED + tick * 16))
      list = list.filter((x) => !e.has(x.node))
      expect(total(list)).toBeLessThanOrEqual(150_000)
      list.push(...Array.from({ length: 3 }, (_, k) => ({ node: 1000 + tick * 3 + k, pts: 8000, since: SETTLED + tick * 16 })))
    }
  })
  it('force ignores the band and releases at least the requested points (allocation failure, M05-FR-026)', () => {
    const list = Array.from({ length: 10 }, (_, i) => ({ node: i + 1, pts: 3000 }))
    expect(plan(cands(list), 30_000, 100_000)).toEqual([])
    const e = plan(cands(list, true, 7000), 30_000, 100_000)
    expect(e.length * 3000).toBeGreaterThanOrEqual(7000)
    expect(e).toHaveLength(3)
  })
  it('a zero budget still yields a finite plan that empties the candidates', () => {
    expect(plan(cands([{ node: 1 }, { node: 2 }]), 2000, 0)).toEqual([1, 2])
  })
})

describe('the same inputs produce the same plan', () => {
  const list: C[] = Array.from({ length: 60 }, (_, i) => ({ node: i + 1, pts: 1000 + ((i * 37) % 900), level: i % 5, dist: (i * 13) % 70, outside: i % 7 === 0,
    since: i % 3 === 0 ? SETTLED - 5 : 0 }))
  it('repeats across calls', () => {
    expect(plan(cands(list), 300_000, 100_000)).toEqual(plan(cands(list), 300_000, 100_000))
  })
  it('does not depend on the order the candidates arrive in', () => {
    const rev = [...list].reverse()
    const shuffled = list.map((x, i) => [((i * 7919) % 61) / 61, x] as const).sort((a, b) => a[0] - b[0]).map((x) => x[1])
    const a = plan(cands(list), 300_000, 100_000)
    expect(plan(cands(rev), 300_000, 100_000)).toEqual(a)
    expect(plan(cands(shuffled), 300_000, 100_000)).toEqual(a)
  })
  it('does not mutate its input', () => {
    const c = cands(list)
    const copy = { node: c.node.slice(), pts: c.pts.slice(), level: c.level.slice(), dist: c.dist.slice(), outside: c.outside.slice() }
    plan(c, 300_000, 100_000)
    expect(c.node).toEqual(copy.node)
    expect(c.pts).toEqual(copy.pts)
    expect(c.level).toEqual(copy.level)
    expect(c.dist).toEqual(copy.dist)
    expect(c.outside).toEqual(copy.outside)
  })
})
