// stores/mission (M10-FR-066, M10-AC-033): status batching <= 4 Hz, newer-wins dedupe, zero-copy path buffers,
// R24 detail conversion for the M06 overlay. The S1 fixture is two helix missions streaming status at 10 Hz.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  decodeGridU8, decodePolyline4, decodeStatus, flushMission, ingestMissionData, isNewer, missionStore, resetMissionStore,
  selectMissionRows, selectPathOf, toDetail, FLUSH_MIN_MS,
} from '@/stores/mission'
import { storeWriteCounts } from '@/lib/createStore'
import type { DataMsg } from '@/net/rt'

function status(mid: string, state: string, rev: number, tNs: number, item: number, pct: number): DataMsg {
  return {
    topic: `mission/${mid}/status`, channelId: 100, seq: 0, tSimMs: tNs / 1e6,
    data: { mid, state, progress_pct: pct, revision: rev, t_ns: tNs, eta_s: 10,
            tracks: [{ vehicle_id: mid === 'm-lower' ? 'p600-01' : 'p600-02', state: 'WORKING', item, total: 1 }],
            metrics: { facade_coverage: pct / 100 }, plan: { pending: false, last_ms: 12.5 } },
  }
}

function blob(points: number[][]): ArrayBuffer {
  const buf = new ArrayBuffer(16 + points.length * 16)
  const dv = new DataView(buf)
  dv.setUint8(0, 0x41); dv.setUint8(1, 0x57); dv.setUint8(2, 0x52); dv.setUint8(3, 0x42)
  dv.setUint16(4, 1, true); dv.setUint16(6, 2, true); dv.setUint32(8, points.length, true); dv.setUint32(12, 4, true)
  const f = new Float32Array(buf, 16)
  points.forEach((p, i) => f.set(p, 4 * i))
  return buf
}

beforeEach(() => {
  vi.useFakeTimers()
  resetMissionStore()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('mission store', () => {
  it('decodes awr.mission.status.v1 and keeps the newer sample', () => {
    const a = decodeStatus(status('m-lower', 'RUNNING', 1, 2e9, 0, 10).data)!
    expect(a).toMatchObject({ mid: 'm-lower', state: 'RUNNING', progressPct: 10, etaS: 10, revision: 1, tNs: 2e9 })
    expect(a.tracks[0]).toEqual({ vehicleId: 'p600-01', state: 'WORKING', item: 0, total: 1 })
    expect(a.metrics?.facadeCoverage).toBeCloseTo(0.1)
    const older = decodeStatus(status('m-lower', 'RUNNING', 1, 1e9, 0, 5).data)!
    expect(isNewer(older, a)).toBe(false)
    expect(isNewer(a, older)).toBe(true)
    expect(decodeStatus({ mid: 'x', state: 'BOGUS' })).toBeNull()
  })

  it('replays the S1 fixture: <= 4 writes per second and final rows match', () => {
    const before = storeWriteCounts().mission ?? 0
    // 20 s of 10 Hz status for two missions (400 messages), progress rising, then DONE
    for (let k = 0; k < 200; k++) {
      const t = k * 1e8
      ingestMissionData(status('m-lower', 'RUNNING', 1, t, 0, k / 2))
      ingestMissionData(status('m-upper', 'RUNNING', 1, t, 0, k / 2.5))
      vi.advanceTimersByTime(100)
    }
    ingestMissionData(status('m-lower', 'DONE', 2, 21e9, 1, 100))
    ingestMissionData(status('m-upper', 'DONE', 2, 21e9, 1, 100))
    // an out-of-order older sample must not win
    ingestMissionData(status('m-upper', 'RUNNING', 1, 20e9, 0, 50))
    vi.advanceTimersByTime(FLUSH_MIN_MS)
    const writes = (storeWriteCounts().mission ?? 0) - before
    expect(writes).toBeGreaterThan(0)
    expect(writes).toBeLessThanOrEqual(Math.ceil(20.25 * 4) + 1)
    const rows = selectMissionRows(missionStore.getState())
    expect(rows.map((r) => [r.mid, r.state, r.progressPct])).toEqual([['m-lower', 'DONE', 100], ['m-upper', 'DONE', 100]])
    expect(selectMissionRows(missionStore.getState())).toBe(rows) // stable reference while unchanged
  })

  it('keeps path buffers zero-copy and clears on an empty path', () => {
    const buf = blob([[0, 0, 10, 0], [5, 0, 10, 1], [10, 0, 10, 2]])
    ingestMissionData({ topic: 'uav/p600-01/path', channelId: 101, seq: 0, tSimMs: 0, data: buf })
    flushMission()
    const pts = selectPathOf('p600-01')(missionStore.getState())!
    expect(pts.buffer).toBe(buf)
    expect(Array.from(pts.slice(4, 8))).toEqual([5, 0, 10, 1])
    vi.advanceTimersByTime(FLUSH_MIN_MS)
    ingestMissionData({ topic: 'uav/p600-01/path', channelId: 101, seq: 1, tSimMs: 0, data: blob([]) })
    vi.advanceTimersByTime(FLUSH_MIN_MS)
    expect(selectPathOf('p600-01')(missionStore.getState())).toBeNull()
  })

  it('rejects malformed blobs', () => {
    expect(decodePolyline4(new ArrayBuffer(8))).toBeNull()
    const b = blob([[1, 2, 3, 4]])
    new DataView(b).setUint16(6, 3, true)
    expect(decodePolyline4(b)).toBeNull()
  })

  it('decodes coverage snapshots (grid_u8) zero-copy and takes the geometry from R24', () => {
    const buf = new ArrayBuffer(16 + 6)
    const dv = new DataView(buf)
    dv.setUint8(0, 0x41); dv.setUint8(1, 0x57); dv.setUint8(2, 0x52); dv.setUint8(3, 0x42)
    dv.setUint16(4, 1, true); dv.setUint16(6, 3, true); dv.setUint32(8, 6, true); dv.setUint32(12, 1, true)
    new Uint8Array(buf, 16).set([0, 1, 2, 0, 3, 1])
    const owner = decodeGridU8(buf)!
    expect(owner.buffer).toBe(buf)
    expect(Array.from(owner)).toEqual([0, 1, 2, 0, 3, 1])
    const bad = buf.slice(0)
    new DataView(bad).setUint16(6, 2, true)
    expect(decodeGridU8(bad)).toBeNull()
    const detail = new Map(missionStore.getState().detail)
    detail.set('m-a', toDetail({ mid: 'm-a', revision: 1, generator: 'lawnmower',
                                 coverage_grid: { x0_m: 0, y0_m: 0, res_m: 4, w: 3, h: 2, k: 2 } }))
    missionStore.setState({ detail })
    ingestMissionData({ topic: 'mission/m-a/coverage', channelId: 102, seq: 0, tSimMs: 0, data: buf })
    flushMission()
    const cov = missionStore.getState().coverage.get('m-a')!
    expect(cov.owner.buffer).toBe(buf)
    expect(cov.geom).toEqual({ x0M: 0, y0M: 0, resM: 4, w: 3, h: 2 })
  })

  it('converts R24 detail into overlay waypoints, areas and slots', () => {
    const d = toDetail({
      mid: 'm-a', revision: 3, generator: 'lawnmower', region: [[0, 0], [10, 0], [10, 10]],
      tracks: [{ vehicle_id: 'p600-01', cursor: 1, items: [{ pos: [0, 0, 50] }, { pos: [5, 5, 50] }, { pos: [9, 9, 50] }] }],
      formation: { slots_flu: [[1, 2, 0], [-1, -2, 0]], z_m: 80 },
      coverage_grid: { x0_m: -2, y0_m: -2, res_m: 2, w: 8, h: 8 },
    })
    expect(d.waypoints.map((w) => w.state)).toEqual(['reached', 'current', 'planned'])
    expect(Array.from(d.areas[0].ring)).toEqual([0, 0, 10, 0, 10, 10])
    expect(d.slots).toEqual([{ x: 1, y: 2, z: 80 }, { x: -1, y: -2, z: 80 }])
    expect(d.coverageGrid).toEqual({ x0M: -2, y0M: -2, resM: 2, w: 8, h: 8 })
  })
})
