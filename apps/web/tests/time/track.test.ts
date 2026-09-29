// M12-AC-024 ticks (spans 2 s to 2 h: labels >= 64 px apart, only layers >= 3 px), ColumnAgg per pixel column (count,
// highest-priority class, representative), M12-AC-019 one red on the model (3 critical markers -> exactly one HERO;
// dwell 1.5 s; acknowledged and superseded markers never win), marker cap (INFO dropped first), jump targets.
import { describe, expect, it } from 'vitest'
import { INPUT } from '@/lib/tokens/input.gen'
import { MarkerClass, MARKER_SUPERSEDED, TIME_PARAMS, TrackModel, markerClassOf, timelineTicks } from '@/engine/time/index'
import { lcg } from './helpers'

describe('tick plan (M12-AC-024)', () => {
  it('keeps labels >= 64 px apart and draws only layers >= 3 px over 20 spans', () => {
    const spans: number[] = []
    for (let i = 0; i < 20; i++) spans.push(2 * Math.pow(3600, i / 19))
    for (const w of [1920, 1280, 600]) {
      for (const span of spans) {
        const t = timelineTicks(span, w)
        expect((t.main * w) / span, `${span} s @ ${w}`).toBeGreaterThanOrEqual(64 - 1e-9)
        if (t.sub !== undefined) {
          expect((t.sub * w) / span).toBeGreaterThanOrEqual(3)
          expect(Math.abs(t.main / t.sub - Math.round(t.main / t.sub))).toBeLessThan(1e-9)
        }
        if (t.tiny !== undefined) expect((t.tiny * w) / span).toBeGreaterThanOrEqual(3)
      }
    }
    expect(timelineTicks(60, 1920).main).toBe(5)
  })
})

describe('markers and columns', () => {
  it('maps event types and levels to classes like markers.json', () => {
    expect(markerClassOf('sim.restarted', 2)).toBe(MarkerClass.WARNING)
    expect(markerClassOf('safety.geofence', 3)).toBe(MarkerClass.CRITICAL)
    expect(markerClassOf('uav.state', 0, { to: 'TAKING_OFF' })).toBe(MarkerClass.LIFECYCLE)
    expect(markerClassOf('uav.state', 0, { to: 'FLYING' })).toBe(MarkerClass.NONE)
    expect(markerClassOf('cmd.accepted', 1, { op: 'goto' })).toBe(MarkerClass.ROUTE)
    expect(markerClassOf('cmd.accepted', 1, { op: 'hover' })).toBe(MarkerClass.NONE)
    expect(markerClassOf('rec.started', 1)).toBe(MarkerClass.SYSTEM)
    expect(markerClassOf('mission.state', 1)).toBe(MarkerClass.ROUTE)
  })

  it('aggregates 50k markers per pixel column with the highest priority and a stable buffer', () => {
    const m = new TrackModel()
    const rnd = lcg(9)
    for (let i = 0; i < 50_000; i++) {
      const lv = rnd() < 0.01 ? 3 : rnd() < 0.05 ? 2 : 0
      const cls = lv === 3 ? MarkerClass.CRITICAL : lv === 2 ? MarkerClass.WARNING : MarkerClass.LIFECYCLE
      m.addMarker(i * 72, lv, cls, 0xffff, i)
    }
    const a = m.columns(0, 3600, 1920)
    let sum = 0
    for (let c = 0; c < a.n; c++) {
      sum += a.count[c]
      if (a.count[c] > 0) expect(m.markers.marker[a.repIdx[c]] & 15).toBe(a.maxClass[c])
    }
    expect(sum).toBe(50_000)
    expect(m.columns(0, 3600, 1920)).toBe(a)
    const hasCritical = Array.from(a.maxClass.subarray(0, a.n)).filter((c) => c === MarkerClass.CRITICAL).length
    expect(hasCritical).toBeGreaterThan(0)
    // out-of-order insertion keeps the order
    m.addMarker(10, 1, MarkerClass.ROUTE, 1, 999_999)
    for (let i = 1; i < m.markers.n; i++) expect(m.markers.t[i]).toBeGreaterThanOrEqual(m.markers.t[i - 1])
  })

  it('drops INFO markers first beyond the cap', () => {
    const m = new TrackModel()
    const cap = TIME_PARAMS.markersCap
    for (let i = 0; i < cap; i++) m.addMarker(i, i % 10 === 0 ? 2 : 0, i % 10 === 0 ? MarkerClass.WARNING : MarkerClass.LIFECYCLE, 0xffff, i)
    expect(m.markers.n).toBe(cap)
    expect(m.addMarker(cap, 0, MarkerClass.LIFECYCLE, 0xffff, cap)).toBe(false)
    expect(m.addMarker(cap, 2, MarkerClass.WARNING, 0xffff, cap)).toBe(true)
    expect(m.markers.n).toBeLessThan(cap)
    let warnings = 0
    for (let i = 0; i < m.markers.n; i++) if (m.markers.level[i] === 2) warnings++
    expect(warnings).toBe(cap / 10 + 1)
  })
})

describe('one red (M12-AC-019, model part)', () => {
  it('3 critical markers give exactly one HERO; the newest wins after the 1.5 s dwell; acknowledged ones never win', () => {
    const m = new TrackModel()
    m.addMarker(1000, 3, MarkerClass.CRITICAL, 1, 11, 0)
    m.evalHero(0)
    expect(m.markers.mseq[m.heroIdx]).toBe(11)
    m.addMarker(2000, 3, MarkerClass.CRITICAL, 2, 12, 100)
    m.addMarker(3000, 3, MarkerClass.CRITICAL, 3, 13, 200)
    m.evalHero(250)
    expect(m.markers.mseq[m.heroIdx]).toBe(11) // dwell
    m.evalHero(250 + INPUT.redDwellMs)
    expect(m.markers.mseq[m.heroIdx]).toBe(13)
    const heroes = [m.heroIdx].filter((x) => x >= 0)
    expect(heroes.length).toBe(1)
    m.isAcked = (s) => s === 13
    m.evalHero(250 + 2 * INPUT.redDwellMs)
    expect(m.markers.mseq[m.heroIdx]).toBe(12)
    m.markSuperseded(1500, 2500)
    expect(m.markers.marker[1] & MARKER_SUPERSEDED).toBe(MARKER_SUPERSEDED)
    m.evalHero(250 + 3 * INPUT.redDwellMs)
    expect(m.markers.mseq[m.heroIdx]).toBe(11)
    m.isAcked = () => true
    m.evalHero(250 + 4 * INPUT.redDwellMs)
    expect(m.heroIdx).toBe(-1)
  })
})

describe('jump targets and series', () => {
  it('finds the next and previous bookmark or >= WARNING marker', () => {
    const m = new TrackModel()
    m.addMarker(5000, 2, MarkerClass.WARNING, 0xffff, 1)
    m.addMarker(9000, 1, MarkerClass.ROUTE, 0xffff, 2)
    m.addMarker(12000, 3, MarkerClass.CRITICAL, 0xffff, 3)
    m.setBookmarks([7, 20])
    expect(m.nextMarkTime(0, 1)).toBe(5)
    expect(m.nextMarkTime(5, 1)).toBe(7)
    expect(m.nextMarkTime(7, 1)).toBe(12)
    expect(m.nextMarkTime(12, 1)).toBe(20)
    expect(m.nextMarkTime(20, 1)).toBeNaN()
    expect(m.nextMarkTime(12, -1)).toBe(7)
    expect(m.nextMarkTime(7, -1)).toBe(5)
    expect(m.nearest(9.001, 0.01)).toBe(1)
  })
  it('appends the altitude series in time order and halves it when full', () => {
    const m = new TrackModel()
    const v0 = m.version
    for (let i = 0; i < TIME_PARAMS.seriesCap + 10; i++) m.pushSeries(i * 250, i)
    expect(m.series.n).toBeLessThanOrEqual(TIME_PARAMS.seriesCap)
    for (let i = 1; i < m.series.n; i++) expect(m.series.t[i]).toBeGreaterThan(m.series.t[i - 1])
    expect(m.version).toBeGreaterThan(v0)
  })
})
