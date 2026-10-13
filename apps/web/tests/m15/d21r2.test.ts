// WP-12c round 2 (after D2-ACC-1, D2-FX-R2): requests d2-WP-13b-to-M15 items 1, 5, 6; d2-WP-13-to-M15 item 1 (fallback
// path); d2-INT-to-M15-M17 item 4; d2-FX-R2-sandbox-to-M15 item 1. Pure parts only (the controller and the layouts are
// covered by perf/m15/d21-shell.spec.ts and d21-mobile.spec.ts).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { itemsFromDetails, RosterTargetGate } from '@/ui/panels/targets/targetModel'
import { QUANT_COMPACT_KEYS, QUANT_COMPACT_PX, quantCompact } from '@/ui/panels/targets/QuantCard'
import { railAltitude } from '@/ui/panels/drones/DronesPanel'
import { targetPos } from '@/ui/mobile/QuickCommand'
import { targetsModel } from '@/app/shell/engineD2'
import { freezableInterval, setPowerFrozen } from '@/lib/powerFreeze'
import { PERF_SETTLE_MS, powerStore } from '@/app/power/store'

const cfg = (id: string, pos?: (number | null)[]) => ({ id, label: id, class: 'person', ...(pos ? { pos } : {}) })

describe('targets list fallback: the roster is authoritative for the ids it listed (d2-WP-13-to-M15 item 1)', () => {
  it('keeps seeded and freshly added ids until the roster lists them, drops ids that left the roster', () => {
    const gate = new RosterTargetGate()
    const cfgs = new Map([['person-01', cfg('person-01')], ['object-01', cfg('object-01')]])
    // before the first roster: the seeded configurations show
    let keep = gate.update([])
    expect(itemsFromDetails([], new Map(), cfgs, keep).map((x) => x.id)).toEqual(['person-01', 'object-01'])
    // the roster lists both
    keep = gate.update([{ id: 'person-01' }, { id: 'object-01' }])
    expect(itemsFromDetails([{ id: 'person-01' }, { id: 'object-01' }], new Map(), cfgs, keep).map((x) => x.id)).toEqual(['person-01', 'object-01'])
    // object-01 removed elsewhere (REST): the roster drops it, its cached configuration does not keep the row
    keep = gate.update([{ id: 'person-01' }])
    expect(itemsFromDetails([{ id: 'person-01' }], new Map(), cfgs, keep).map((x) => x.id)).toEqual(['person-01'])
    // a target this page just added (configuration first, roster later) shows at once
    cfgs.set('dog-01', cfg('dog-01'))
    expect(itemsFromDetails([{ id: 'person-01' }], new Map(), cfgs, gate.update([{ id: 'person-01' }])).map((x) => x.id)).toEqual(['person-01', 'dog-01'])
    // a new session starts from nothing seen
    gate.reset()
    expect(itemsFromDetails([], new Map(), cfgs, gate.update([])).map((x) => x.id)).toEqual(['person-01', 'object-01', 'dog-01'])
  })
  it('keeps the old behaviour without a gate', () => {
    const cfgs = new Map([['a', cfg('a')]])
    expect(itemsFromDetails([], new Map(), cfgs).map((x) => x.id)).toEqual(['a'])
  })
})

describe('quick command "watch": target positions (d2-WP-13b-to-M15 item 1)', () => {
  afterEach(() => targetsModel.reset())
  it('takes the store row, else the configured position (ground z 0), else null', () => {
    expect(targetPos('x', { pos: [1, 2, 3] })).toEqual([1, 2, 3])
    targetsModel.setConfig({ id: 'person-01', class: 'person', pos: [10, -20, null] })
    expect(targetPos('person-01')).toEqual([10, -20, 0])
    targetsModel.setConfig({ id: 'uav-01', class: 'uav', pos: [5, 6, 40] })
    expect(targetPos('uav-01', { pos: [Number.NaN, 0, 0] })).toEqual([5, 6, 40])
    expect(targetPos('nobody')).toBeNull()
  })
})

describe('perception readout card columns (d2-WP-13b-to-M15 item 6)', () => {
  it('uses the compact columns below 600 px unless "all columns" is on', () => {
    expect(quantCompact(0, false)).toBe(false)
    expect(quantCompact(344, false)).toBe(true)
    expect(quantCompact(344, true)).toBe(false)
    expect(quantCompact(QUANT_COMPACT_PX, false)).toBe(false)
    expect(QUANT_COMPACT_KEYS).toEqual(['uav', 'ac', 'vis', 'verdict'])
  })
})

describe('fleet rail altitude follows Settings › altitude reference (d2-INT-to-M15-M17 item 4)', () => {
  it('AGL subtracts the terrain under the vehicle, MSL is the world z', () => {
    const ground = vi.fn(() => -18.3)
    expect(railAltitude(-15.8, 'AGL', ground)).toBeCloseTo(2.5, 6)
    expect(railAltitude(-15.8, 'MSL', ground)).toBe(-15.8)
    expect(ground).toHaveBeenCalledTimes(1)
    expect(railAltitude(12, 'AGL', () => Number.NaN)).toBe(12)
    expect(railAltitude(Number.NaN, 'AGL', ground)).toBeNaN()
  })
})

describe('power-save freeze of the shell timers (d2-FX-R2-sandbox-to-M15 item 1)', () => {
  afterEach(() => {
    setPowerFrozen(false)
    vi.useRealTimers()
  })
  it('a 1 Hz interval registered like the controller tick, the URL sync and the store-write roller stops while frozen', () => {
    vi.useFakeTimers()
    let n = 0
    const stop = freezableInterval(() => n++, 1000)
    vi.advanceTimersByTime(3000)
    expect(n).toBe(3)
    setPowerFrozen(true)
    vi.advanceTimersByTime(10_000)
    expect(n).toBe(3)
    setPowerFrozen(false) // runs once at once, then every second again
    expect(n).toBe(4)
    vi.advanceTimersByTime(1000)
    expect(n).toBe(5)
    stop()
  })
  it('the perf figures settle for the 2 s summary window plus one UI tick', () => {
    expect(PERF_SETTLE_MS).toBeGreaterThanOrEqual(2000 + 250)
    expect(powerStore.getState().perfPaused).toBe(false)
  })
})
