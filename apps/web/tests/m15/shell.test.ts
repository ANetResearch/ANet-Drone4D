// M15 shell logic: connection view (banner after INPUT.offlineBannerDelayMs, write guard, role text; AWR-14 §7.7, §7.8),
// the tool state machine (§6.9), the rail order and navigation (M15-FR-024), the World list mapping of R04 (SK-B-to-M15),
// the palette vehicle search, the wind icon unwrap (M15 §14 item 16), histogram bins and governor toast dedup.
import { describe, expect, it, vi } from 'vitest'

// the first import of the viewport facade and three.js is slow on a loaded machine
vi.setConfig({ testTimeout: 30_000 })

describe('connection view', () => {
  it('shows the offline banner only after the delay and clears it when live', async () => {
    vi.useFakeTimers()
    try {
      const { connViewStore, onConnState } = await import('@/ui/shell/connView')
      const { INPUT } = await import('@/lib/tokens/input.gen')
      onConnState('LIVE', null)
      onConnState('RECONNECTING', { attempt: 1, nextInMs: 500 })
      expect(connViewStore.getState()).toMatchObject({ conn: 'RECONNECTING', bannerVisible: false, attempt: 1 })
      vi.advanceTimersByTime(INPUT.offlineBannerDelayMs - 10)
      expect(connViewStore.getState().bannerVisible).toBe(false)
      vi.advanceTimersByTime(20)
      expect(connViewStore.getState().bannerVisible).toBe(true)
      onConnState('LIVE', null)
      expect(connViewStore.getState()).toMatchObject({ bannerVisible: false })
      expect(Number.isNaN(connViewStore.getState().downSinceMs)).toBe(true)
      // a short drop never shows the banner
      onConnState('RECONNECTING', { attempt: 1, nextInMs: 500 })
      vi.advanceTimersByTime(INPUT.offlineBannerDelayMs / 2)
      onConnState('LIVE', null)
      vi.advanceTimersByTime(INPUT.offlineBannerDelayMs)
      expect(connViewStore.getState().bannerVisible).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('derives the write guard and the role text (AWR-14 §4.1, §7.8)', async () => {
    const { canWriteOf, roleKeyOf } = await import('@/ui/shell/connView')
    expect(canWriteOf({ conn: 'LIVE', role: 'operator', seat: 'held', mode: 'live' })).toBe(true)
    expect(canWriteOf({ conn: 'LIVE', role: 'viewer', seat: 'none', mode: 'live' })).toBe(false)
    expect(canWriteOf({ conn: 'LIVE', role: 'operator', seat: 'other', mode: 'live' })).toBe(false)
    expect(canWriteOf({ conn: 'RECONNECTING', role: 'operator', seat: 'held', mode: 'live' })).toBe(false)
    expect(canWriteOf({ conn: 'LIVE', role: 'admin', seat: 'held', mode: 'replay' })).toBe(false)
    expect(roleKeyOf({ role: 'operator', seat: 'held' })).toBe('role.operator')
    expect(roleKeyOf({ role: 'operator', seat: 'other' })).toBe('role.viewerSeatTaken')
    expect(roleKeyOf({ role: 'viewer', seat: 'none' })).toBe('role.viewer')
    expect(roleKeyOf({ role: null, seat: null })).toBe('role.unknown')
  })
})

describe('tool state machine', () => {
  it('steps through GoTo and Add P600 with the Esc levels (M15 §6.9)', async () => {
    const { toolNext } = await import('@/ui/tools/toolMode')
    expect(toolNext('IDLE', 'goto')).toBe('GOTO_PICK')
    expect(toolNext('GOTO_PICK', 'pick')).toBe('GOTO_CONFIRM')
    expect(toolNext('GOTO_CONFIRM', 'cameraMoved')).toBe('GOTO_PICK')
    expect(toolNext('GOTO_CONFIRM', 'escape')).toBe('GOTO_PICK')
    expect(toolNext('GOTO_PICK', 'escape')).toBe('IDLE')
    expect(toolNext('GOTO_CONFIRM', 'confirm')).toBe('IDLE')
    expect(toolNext('IDLE', 'add')).toBe('ADD_PICK')
    expect(toolNext('ADD_PICK', 'pick')).toBe('ADD_CONFIRM')
    expect(toolNext('ADD_CONFIRM', 'escape')).toBe('ADD_PICK')
    expect(toolNext('IDLE', 'pick')).toBe('IDLE')
    expect(toolNext('IDLE', 'escape')).toBe('IDLE')
  })
})

describe('rail order', () => {
  const rows = {
    n: 6,
    agentNo: Uint16Array.from([5, 1, 3, 2, 4, 6]),
    fs: Uint8Array.from([5, 3, 11, 7, 5, 12]),
    battery: Uint8Array.from([80, 255, 10, 50, 90, 30]),
    owner: Uint8Array.from([1, 0, 2, 1, 1, 0]),
    alert: Uint8Array.from([0, 0, 1, 1, 0, 0]),
  }
  const ids = ['p600-05', 'p600-01', 'x500-03', 'p600-02', 'p600-04', 'p600-06']
  const idOf = (i: number) => ids[i]
  const base = { query: '', group: 'all' as const, owner: -1, sort: 'id' as const }

  it('filters and sorts on the typed rows', async () => {
    const { buildOrder } = await import('@/ui/panels/drones/railModel')
    expect(Array.from(buildOrder(rows, base, idOf), idOf)).toEqual(['p600-01', 'p600-02', 'x500-03', 'p600-04', 'p600-05', 'p600-06'])
    expect(Array.from(buildOrder(rows, { ...base, group: 'air' }, idOf), idOf)).toEqual(['p600-02', 'x500-03', 'p600-04', 'p600-05'])
    expect(Array.from(buildOrder(rows, { ...base, group: 'alert' }, idOf), idOf)).toEqual(['p600-02', 'x500-03'])
    expect(Array.from(buildOrder(rows, { ...base, owner: 0 }, idOf), idOf)).toEqual(['p600-01', 'p600-06'])
    expect(Array.from(buildOrder(rows, { ...base, sort: 'severity' }, idOf), idOf).slice(0, 2)).toEqual(['x500-03', 'p600-02'])
    expect(Array.from(buildOrder(rows, { ...base, sort: 'battery' }, idOf), idOf)[0]).toBe('x500-03')
    // prefix matches before substring matches
    expect(Array.from(buildOrder(rows, { ...base, query: '0-0' }, idOf), idOf)).toEqual(['p600-01', 'p600-02', 'x500-03', 'p600-04', 'p600-05', 'p600-06'])
    expect(Array.from(buildOrder(rows, { ...base, query: 'x5' }, idOf), idOf)).toEqual(['x500-03'])
  })

  it('handles 1000 rows and steps through the list with wrap-around', async () => {
    const { buildOrder, stepId } = await import('@/ui/panels/drones/railModel')
    const n = 1000
    const big = { n, agentNo: new Uint16Array(n), fs: new Uint8Array(n), battery: new Uint8Array(n), owner: new Uint8Array(n), alert: new Uint8Array(n) }
    for (let i = 0; i < n; i++) {
      big.agentNo[i] = n - i
      big.fs[i] = i % 14
      big.battery[i] = i % 101
    }
    const order = buildOrder(big, { ...base, sort: 'severity' }, (i) => `uav${String(big.agentNo[i]).padStart(4, '0')}`)
    expect(order.length).toBe(n)
    expect(new Set(order).size).toBe(n)
    expect(stepId(['a', 'b', 'c'], 'c', 1)).toBe('a')
    expect(stepId(['a', 'b', 'c'], 'a', -1)).toBe('c')
    expect(stepId(['a', 'b', 'c'], null, 1)).toBe('a')
    expect(stepId([], 'a', 1)).toBeNull()
  })
})

describe('R04 world list mapping', () => {
  it('maps items and snake_case fields (SK-B-to-M15 item 1)', async () => {
    const { mapWorldList } = await import('@/app/query/options')
    const list = mapWorldList({
      items: [{ id: 'shenzhen', name_zh: 'Shenzhen zh', status: 'ready', content_version: 'abc123def', anchor_kind: 'synthetic', georeferenced: false,
        points: 5_000_000, bytes: 80_000_000, roots: 1, node_count: 1234, levels_points: [10, 20, 30], max_height_m: 381, in_use: true }],
    })
    expect(list[0]).toMatchObject({
      id: 'shenzhen', nameZh: 'Shenzhen zh', status: 'ready', contentVersion: 'abc123def', anchorKind: 'synthetic', georeferenced: false,
      levelsPoints: [10, 20, 30], inUse: true, stats: { points: 5_000_000, nodes: 1234, maxHeightM: 381, roots: 1 },
    })
    expect(mapWorldList([{ id: 'a', status: 'weird' }])[0].status).toBeUndefined()
    expect(mapWorldList({ items: undefined } as never)).toEqual([])
  })
})

describe('command palette vehicle search', () => {
  it('lists prefix matches before substring matches, capped', async () => {
    const { matchVehicles } = await import('@/ui/views/CommandPalette')
    const ids = Array.from({ length: 1000 }, (_, i) => `uav${String(i).padStart(4, '0')}`)
    expect(matchVehicles(ids, 'uav000')).toHaveLength(10)
    expect(matchVehicles(ids, '99')[0]).toBe('uav0099')
    expect(matchVehicles(['p600-01', 'x-p600'], 'p600')).toEqual(['p600-01', 'x-p600'])
    expect(matchVehicles(ids, '')).toEqual([])
    expect(matchVehicles(ids, 'uav', 20)).toHaveLength(20)
  })
})

describe('wind direction icon', () => {
  it('turns the short way, also after two full turns (M15-AC-039)', async () => {
    const { unwrapDeg } = await import('@/ui/panels/env/EnvPanel')
    expect(unwrapDeg(359, 1)).toBe(361)
    expect(unwrapDeg(1, 359)).toBe(-1)
    let a = 0
    for (let i = 0; i < 8; i++) a = unwrapDeg(a, (a + 90) % 360)
    expect(a).toBe(720)
    expect(unwrapDeg(719, 1)).toBe(721)
    expect(unwrapDeg(-719, 359)).toBe(-721)
  })
})

describe('histogram bins', () => {
  it('bins frame intervals and finds the median bin', async () => {
    const { binCounts, medianBin } = await import('@/ui/lf/LfHistogram')
    const edges = [0, 8.3, 16.7, 33.3, 50, 100, Number.POSITIVE_INFINITY]
    const c = binCounts([5, 16.6, 16.7, 33.4, 40, 120, 1000, Number.NaN], 8, edges)
    expect(c).toEqual([1, 1, 1, 2, 0, 2])
    expect(medianBin(c)).toBe(3)
    expect(medianBin([0, 0])).toBe(-1)
  })
})

describe('governor toasts', () => {
  it('toast a degradation once per knob within the dedup window, never a restore (M15-AC-051)', async () => {
    const { onGovernorStep } = await import('@/ui/hud/governorToasts')
    const { INPUT } = await import('@/lib/tokens/input.gen')
    expect(onGovernorStep({ step: 1, dir: -1, reasonKey: 'perf.governor.trails' }, 1000)).toBe(true)
    expect(onGovernorStep({ step: 1, dir: -1, reasonKey: 'perf.governor.trails' }, 2000)).toBe(false)
    expect(onGovernorStep({ step: 1, dir: 1, reasonKey: 'perf.governor.trails' }, 3000)).toBe(false)
    expect(onGovernorStep({ step: 2, dir: -1, reasonKey: 'perf.governor.frustums' }, 3000)).toBe(true)
    expect(onGovernorStep({ step: 1, dir: -1, reasonKey: 'perf.governor.trails' }, 1000 + INPUT.governorToastDedupMs + 1)).toBe(true)
  })
})
