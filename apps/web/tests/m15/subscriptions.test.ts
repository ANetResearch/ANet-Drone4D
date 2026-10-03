// Subscription granularity (ADR-072; AWR-18 §6.1 item 9; D1-AC-23): the timeline canvases take their moving inputs in the
// scheduler (a redraw only when the view, the playhead or the range changed by value), DroneRail rows re-render on their
// own signature, not on every fleet sample, and the ViewCube only shows faces that face the viewer.
import { describe, expect, it } from 'vitest'
import { applyInputs, type DrawState } from '@/ui/lf/LfTimelineTrack'
import { railKey, rowSignature } from '@/ui/panels/drones/DronesPanel'
import { fleetRows } from '@/stores/fleet'

describe('timeline track inputs read in the scheduler', () => {
  it('bump the revision only when a value changed', () => {
    const st: DrawState = { view: { t0S: 0, t1S: 60 }, playheadS: 10, range: { t0S: 0, t1S: 60 }, rev: 0 }
    expect(applyInputs(st, { t0S: 0, t1S: 60 }, 10, { t0S: 0, t1S: 60 })).toBe(false)
    expect(st.rev).toBe(0)
    expect(applyInputs(st, { t0S: 0.25, t1S: 60.25 }, 10.25, { t0S: 0, t1S: 60.25 })).toBe(true)
    expect(st.rev).toBe(1)
    // a fresh object with the same values (every read builds one) is no change
    expect(applyInputs(st, { t0S: 0.25, t1S: 60.25 }, 10.25, { t0S: 0, t1S: 60.25 })).toBe(false)
    expect(st.rev).toBe(1)
  })
})

describe('DroneRail row signature', () => {
  it('changes with what the row shows and nothing else', () => {
    const i = 3
    fleetRows.fs[i] = 7
    fleetRows.sub[i] = 2
    fleetRows.flags[i] = 3
    fleetRows.battery[i] = 80
    fleetRows.owner[i] = 1
    fleetRows.alert[i] = 0
    fleetRows.stale[i] = 0
    const a = rowSignature(i)
    expect(rowSignature(i)).toBe(a)
    fleetRows.battery[i] = 79
    const b = rowSignature(i)
    expect(b).not.toBe(a)
    fleetRows.stale[i] = 1
    expect(rowSignature(i)).not.toBe(b)
    fleetRows.alert[i] = 1
    fleetRows.owner[i] = 2
    const c = rowSignature(i)
    expect(new Set([a, b, c]).size).toBe(3)
    expect(Number.isSafeInteger(c)).toBe(true)
  })
})

describe('DroneRail key', () => {
  it('follows the row count and the row signatures only', () => {
    for (let i = 0; i < 4; i++) {
      fleetRows.fs[i] = 6
      fleetRows.battery[i] = 90 - i
      fleetRows.stale[i] = 0
      fleetRows.alert[i] = 0
    }
    const k4 = railKey({ n: 4 })
    expect(railKey({ n: 4 })).toBe(k4)
    expect(railKey({ n: 3 })).not.toBe(k4)
    fleetRows.battery[2] = 50
    expect(railKey({ n: 4 })).not.toBe(k4)
  })
})
