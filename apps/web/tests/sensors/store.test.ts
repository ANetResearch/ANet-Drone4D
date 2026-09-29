// M13-FR-080, FR-081, M13-AC-026 (Node part): stores/sensors.ts field builders, the 4 Hz feed tick, state_ext parsing,
// sanitised detection rows and the write budget (no write when nothing changed).
import { afterEach, describe, expect, it } from 'vitest'
import type { FrameCtx } from '@/engine/loop'
import type { DataMsg, RtClient, RtEvent } from '@/net/rt/types'
import * as S from '@/engine/sensors'
import { selection } from '@/stores/selection'
import {
  attachSensorsFeed, buildSensorRows, detectionRows, frameRectFraction, gimbalModesFromExt, gnssFromExt, imuFromExt, sensorsFeedTick, sensorsStore,
} from '@/stores/sensors'

function fakeRt() {
  const subs = new Set<string>()
  let dataCb: ((m: DataMsg) => void) | null = null
  let evCb: ((b: readonly RtEvent[]) => void) | null = null
  const rt = {
    roster: {
      get: (a: number) => (a === 3 ? { agentNo: 3, id: 'p600-01', model: 'p600' } : undefined),
      idOf: (a: number) => (a === 3 ? 'p600-01' : undefined),
      agentNoOf: (id: string) => (id === 'p600-01' ? 3 : -1),
    },
    subscribe: (t: string) => {
      subs.add(t)
      return () => subs.delete(t)
    },
    onData: (cb: (m: DataMsg) => void) => {
      dataCb = cb
      return () => { dataCb = null }
    },
    onEvents: (cb: (b: readonly RtEvent[]) => void) => {
      evCb = cb
      return () => { evCb = null }
    },
    onTime: () => () => {},
  } as unknown as RtClient
  return { rt, subs, data: (m: Partial<DataMsg>) => dataCb?.(m as DataMsg), events: (e: Partial<RtEvent>) => evCb?.([e as RtEvent]) }
}

const EXT = {
  loc: { status: 'TRACKING', gnss_fix: 4, sats: 22, eph_m: 0.0158, epv_m: 0.018, hdop: 0.8, err_enu_m: [0.003, -0.004, 0.01] },
  sens: {
    gimbal: [{ sensor_no: 0, mode: 'nadir', az_rad: 0, el_rad: -1.5708, limited: false }, { sensor_no: 1, mode: 'look_at', az_rad: 0.2, el_rad: -0.5, limited: true }],
    imu: { acc_mps2: [0.1, 0, 9.8], gyro_rad_s: [0, 0, 0.01], bias_acc_mps2: [0.1, 0, 0.2], bias_gyro_rad_s: [0.001, 0, 0] },
  },
}

afterEach(() => {
  S.uninstall()
  selection.select([])
})

describe('stores/sensors builders', () => {
  it('parse state_ext loc and sens', () => {
    const g = gnssFromExt(EXT)
    expect(g).toMatchObject({ fix: 'RTK_FIXED', fixCode: 4, sats: 22, ephM: 0.0158, epvM: 0.018, hdop: 0.8 })
    expect(g?.errHM).toBeCloseTo(0.005, 12)
    expect(gnssFromExt({ loc: { status: 'TRACKING', gnss_fix: null } })).toBeNull()
    expect(gnssFromExt({ loc: { gnss_fix: 0, sats: 0, eph_m: null, epv_m: null, hdop: null } })).toMatchObject({ fix: 'NO_FIX', ephM: null })
    expect(imuFromExt(EXT)?.accMps2).toEqual([0.1, 0, 9.8])
    expect(imuFromExt({})).toBeNull()
    expect(gimbalModesFromExt(EXT).get(1)).toEqual({ mode: 'look_at', limited: true })
  })
  it('sensor rows cover every sensor of the vehicle, FOV from the intrinsics', () => {
    const rows = buildSensorRows('p600', [])
    expect(rows.map((r) => [r.name, r.kind, r.state])).toEqual([
      ['camera', 'camera', 'online'], ['thermal', 'thermal', 'online'], ['mid360', 'lidar', 'planned'], ['gnss', 'gnss', 'online'], ['imu', 'imu', 'online'],
    ])
    expect(rows[0].hfovDeg).toBeCloseTo(60, 3)
    expect(rows[0].vfovDeg).toBeCloseTo(42.103, 3)
    expect(rows[3].hfovDeg).toBeNull()
    expect(rows[2].vfovDeg).toBeCloseTo(59.4, 6)
  })
  it('frame rect as viewport fractions', () => {
    S.cache.modelOf = () => 'p600'
    const cam = S.cache.sensorsOf(1)[0]
    const r = frameRectFraction(cam, 16 / 9)
    expect(r?.x).toBeCloseTo((1 - 0.84375) / 2, 12)
    expect(r?.w).toBeCloseTo(0.84375, 12)
    expect(r?.h).toBe(1)
    expect(frameRectFraction(cam, 1.5)).toBeNull()
  })
  it('detection rows are sanitised', () => {
    const d = { targetId: 't1\u{1F600}', kind: 'person', uav: 'a1', sensor: 'camera', capability: 'rgb.zoom', state: 'suspect' as const, conf: 0.42, pos: Float64Array.of(1, 2, 3), rangeM: 60, pd: 0.5, repeat: false, tSimMs: 1, seq: 1, count: 1 }
    expect(detectionRows([d])[0]).toMatchObject({ targetId: 't1', posEnuM: [1, 2, 3], conf: 0.42 })
  })
})

describe('stores/sensors feed', () => {
  it('fills the selected vehicle at <= 4 Hz and only writes on change', () => {
    const f = fakeRt()
    S.ensureInstalled(() => f.rt)
    const detach = attachSensorsFeed(() => f.rt)
    selection.select(['p600-01'])
    const ctx = { cssW: 1280, cssH: 720 } as unknown as FrameCtx
    sensorsFeedTick(ctx, f.rt)
    expect(f.subs.has('uav/p600-01/state_ext')).toBe(true)
    f.data({ topic: 'uav/p600-01/state_ext', data: EXT })
    sensorsFeedTick(ctx, f.rt)
    const s = sensorsStore.getState()
    expect(s.vehicleId).toBe('p600-01')
    expect(s.sensors.length).toBe(5)
    expect(s.gimbal).toMatchObject({ mode: 'nadir', limited: false })
    expect(s.gimbal?.pitchDeg).toBeCloseTo(-15, 9) // no SensorPose48 yet: the default gimbal of the spec
    expect(s.gnss?.fix).toBe('RTK_FIXED')
    expect(s.imu?.gyroRadS[2]).toBe(0.01)
    expect(s.frameRect?.w).toBeCloseTo(0.84375, 12)
    const v = s.version
    sensorsFeedTick(ctx, f.rt)
    expect(sensorsStore.getState().version).toBe(v)
    f.events({ type: 'sensor.detect', t_sim_ns: 1e9, seq: 1, data: { target_id: 't1', target_kind: 'person', uav: 'a1', state: 'suspect', conf: 0.42, pos_enu_m: [1, 2, 0] } })
    sensorsFeedTick(ctx, f.rt)
    expect(sensorsStore.getState().detections.map((d) => [d.targetId, d.state])).toEqual([['t1', 'suspect']])
    selection.select([])
    sensorsFeedTick(ctx, f.rt)
    expect(sensorsStore.getState().vehicleId).toBeNull()
    expect(f.subs.has('uav/p600-01/state_ext')).toBe(false)
    detach()
  })
})
