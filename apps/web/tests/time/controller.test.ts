// Transport logic (M12 §6.5; FR-014 to FR-016, FR-022, FR-047): the guard table against AWR-14 §6.17 for all 10 states
// (live) and the replay statuses, the pending machine (confirm, reject, 1 s timeout, epoch), step ticks, replay step
// targets on the block grid (M12-AC-047: 25 x Shift+. = 1.00 s), and the 2 s RTF badge hysteresis.
import { describe, expect, it } from 'vitest'
import { PendingMachine, RtfWatch, guards, liveStepMs, replayStepTarget, TIME_STATE as TS, type GuardInput } from '@/engine/time/index'

const base: GuardInput = { mode: 'live', canWrite: true, caps: { pausable: true, steppable: true, maxSpeed: 10 }, state4: TS.PAUSED, playbackStatus: null, speedMax: 20 }

describe('guards (M12-AC-022 logic, AWR-14 §6.17)', () => {
  it('follows the live state table for 0..9', () => {
    const row = (s: number) => {
      const g = guards({ ...base, state4: s })
      return [g.play === null, g.step === null, g.speed(1) === null]
    }
    expect(row(TS.STOPPED)).toEqual([true, false, true])
    expect(row(TS.PLAYING)).toEqual([true, false, true])
    expect(row(TS.PAUSED)).toEqual([true, true, true])
    expect(row(TS.STEPPING)).toEqual([true, false, true])
    expect(row(TS.STALLED)).toEqual([false, false, false])
    expect(row(TS.RESTARTING)).toEqual([false, false, false])
    expect(row(TS.FAILED)).toEqual([false, false, false])
    expect(row(TS.LIVE)).toEqual([false, false, false])
    expect(guards({ ...base, state4: TS.LIVE }).play).toBe('hint.clockLive')
  })
  it('greys everything for viewers and follows caps (pausable, steppable, max_speed)', () => {
    const v = guards({ ...base, canWrite: false })
    expect([v.play, v.step, v.speed(1)]).toEqual(['hint.needSeat', 'hint.needSeat', 'hint.needSeat'])
    expect(guards({ ...base, caps: { pausable: false, steppable: true, maxSpeed: 10 } }).play).toBe('hint.clockLocked')
    expect(guards({ ...base, caps: { pausable: true, steppable: false, maxSpeed: 10 } }).step).toBe('hint.clockLocked')
    expect(guards({ ...base, caps: { pausable: true, steppable: true, maxSpeed: 2 } }).speed(5)).toBe('hint.maxSpeed')
    expect(guards(base).seek).toBe('hint.liveNoRewind')
  })
  it('replay controls need the seat and an open player; speeds above speed_max are greyed', () => {
    const r = { ...base, mode: 'replay' as const, playbackStatus: 'paused', speedMax: 8.77 }
    expect(guards(r).seek).toBeNull()
    expect(guards(r).speed(10)).toBe('hint.replaySpeedMax')
    expect(guards(r).speed(5)).toBeNull()
    expect(guards({ ...r, playbackStatus: 'error' }).play).toBe('hint.replayNotOpen')
    expect(guards({ ...r, canWrite: false }).play).toBe('hint.replaySeat')
  })
})

describe('pending machine (FR-014)', () => {
  const from = { state4: TS.PAUSED, rate: 1, tSimMs: 1000, epoch: 3 }
  it('confirms when TIME reaches the target', () => {
    const m = new PendingMachine()
    m.start('play', TS.PLAYING, 0, from)
    expect(m.observe(TS.PAUSED, 1, 1000, 3, 100)).toBeNull()
    expect(m.observe(TS.PLAYING, 1, 1000, 3, 150)).toBe('confirmed')
    expect(m.pending).toBeNull()
  })
  it('times out after 1 s, drops on a new epoch, and settles speed and step by the call result', () => {
    const m = new PendingMachine()
    m.start('pause', TS.PAUSED, 0, { ...from, state4: TS.PLAYING })
    expect(m.observe(TS.PLAYING, 1, 1000, 3, 999)).toBeNull()
    expect(m.observe(TS.PLAYING, 1, 1000, 3, 1000)).toBe('timeout')
    m.start('play', TS.PLAYING, 0, from)
    expect(m.observe(TS.PAUSED, 1, 0, 4, 10)).toBe('epoch')
    m.start('speed', 10, 0, from)
    expect(m.observe(TS.PLAYING, 3.4, 1000, 3, 10)).toBeNull()
    expect(m.settle(true)?.control).toBe('speed')
    expect(m.pending).toBeNull()
    m.start('step', 1100, 0, from)
    expect(m.observe(TS.STEPPING, 1, 1040, 3, 10)).toBeNull()
    expect(m.observe(TS.PAUSED, 1, 1100, 3, 30)).toBe('confirmed')
    m.start('seek', 5000, 0, from)
    expect(m.observe(TS.PAUSED, 1, 0, 9, 5000)).toBeNull()
    m.seekDone()
    expect(m.pending).toBeNull()
  })
})

describe('steps (FR-016, FR-047)', () => {
  it('maps live keys to 25, 250 and 1 ticks', () => {
    expect([liveStepMs('100ms'), liveStepMs('1s'), liveStepMs('tick')]).toEqual([100, 1000, 4])
  })
  it('25 x Shift+. advances 1.00 s on the 40 ms block grid (M12-AC-047), clamped to the data range', () => {
    let t = 12.345
    for (let i = 0; i < 25; i++) t = replayStepTarget('tick', t, 0.04, 0, 600)
    expect(t).toBeCloseTo(12.32 + 1.0, 9)
    expect(replayStepTarget('100ms', 5, 0.04, 0, 600)).toBe(6)
    expect(replayStepTarget('1s', 5, 0.04, 0, 600)).toBe(15)
    expect(replayStepTarget('-1s', 0.5, 0.04, 0, 600)).toBe(0)
    expect(replayStepTarget('-sample', 1.0, 0.04, 0, 600)).toBeCloseTo(0.96, 9)
    expect(replayStepTarget('10s', 595, 0.04, 0, 600)).toBe(600)
    expect(replayStepTarget('sample', 3.0, 0.2, 0, 600)).toBeCloseTo(3.2, 9)
  })
})

describe('RTF badge (FR-015)', () => {
  it('appears after 2 s below 0.95 x requested and clears 2 s after recovery', () => {
    const w = new RtfWatch()
    expect(w.update(3.4, 10, true, 0)).toBe(false)
    expect(w.update(3.4, 10, true, 1999)).toBe(false)
    expect(w.update(3.4, 10, true, 2000)).toBe(true)
    expect(w.update(10, 10, true, 2500)).toBe(true)
    expect(w.update(10, 10, true, 4500)).toBe(false)
    expect(w.update(3.4, 10, false, 5000)).toBe(false)
  })
})
