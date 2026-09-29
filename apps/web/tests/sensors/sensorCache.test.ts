// M13-FR-021, FR-022, FR-081 (facade side): specs from specs.gen.ts, SensorPose48 ingestion from a TelemetryFrame slot,
// body orientation history from swarm and Full64 items, pose topic subscriptions for the wanted vehicles, detections.
import { afterEach, describe, expect, it } from 'vitest'
import { SP48, DS64 } from '@awr/contracts/layouts'
import { H, REGION, RAW_SCHEMA, SLOT_BYTES, SlotReader, SlotWriter } from '@/net/rt/frame'
import type { RtClient, RtEvent } from '@/net/rt/types'
import { runPhase, type FrameCtx } from '@/engine/loop'
import * as S from '@/engine/sensors'

interface FakeRt {
  rt: RtClient
  subs: Set<string>
  emit(ev: Partial<RtEvent>): void
}
function fakeRt(): FakeRt {
  const subs = new Set<string>()
  let evcb: ((b: readonly RtEvent[]) => void) | null = null
  const rt = {
    roster: { get: (a: number) => (a > 0 ? { agentNo: a, id: `v${a}`, model: a >= 9 ? 'x500' : 'p600' } : undefined), idOf: (a: number) => `v${a}` },
    subscribe: (t: string) => {
      subs.add(t)
      return () => subs.delete(t)
    },
    onEvents: (cb: (b: readonly RtEvent[]) => void) => {
      evcb = cb
      return () => { evcb = null }
    },
    onTime: () => () => {},
  } as unknown as RtClient
  return { rt, subs, emit: (ev) => evcb?.([ev as RtEvent]) }
}

function slot(opts: { swarm?: [number, number[]][]; swarmT?: number; full?: [number, number, number[]][]; poses?: [number, number, number, number[], number[], number][] }): SlotReader {
  const w = new SlotWriter()
  const sw = opts.swarm ?? []
  sw.forEach(([a, q], i) => {
    w.swarm.agentNo[i] = a
    for (let k = 0; k < 4; k++) w.swarm.quat[4 * i + k] = q[k]
  })
  w.dv.setUint32(H.swarmN, sw.length, true)
  w.dv.setFloat64(H.swarmTSimMs, opts.swarmT ?? 0, true)
  const full = opts.full ?? []
  full.forEach(([a, t, q], k) => {
    const src = new Uint8Array(64)
    const dv = new DataView(src.buffer)
    dv.setUint16(0, a, true)
    for (let j = 0; j < 4; j++) dv.setFloat32(DS64.Q + 4 * j, q[j], true)
    w.writeFull(k, a, 1, 1, t, src, 0)
  })
  w.dv.setUint32(H.fullCount, full.length, true)
  const poses = opts.poses ?? []
  poses.forEach(([a, sensorNo, t, pos, q, flags], k) => {
    const src = new Uint8Array(48)
    const dv = new DataView(src.buffer)
    dv.setUint16(SP48.AGENT_NO, a, true)
    dv.setUint8(SP48.SENSOR_NO, sensorNo)
    dv.setUint8(SP48.KIND, sensorNo === 0 ? 0 : 2)
    dv.setUint8(SP48.FLAGS, flags)
    for (let j = 0; j < 3; j++) dv.setFloat32(SP48.POS + 4 * j, pos[j], true)
    for (let j = 0; j < 4; j++) dv.setFloat32(SP48.Q + 4 * j, q[j], true)
    dv.setFloat32(SP48.HFOV_RAD, 1.047, true)
    w.writeRaw(k, a, 3, RAW_SCHEMA.SENSOR_POSE48, t, src, 0, 48)
  })
  w.dv.setUint32(H.rawCount, poses.length, true)
  const buf = new ArrayBuffer(SLOT_BYTES)
  w.copyTo(buf)
  expect(REGION.end).toBeLessThanOrEqual(SLOT_BYTES)
  return new SlotReader(buf)
}

afterEach(() => S.uninstall())

describe('engine/sensors facade', () => {
  it('builds views from the generated specs (p600 camera + thermal + lidar, x500 camera)', () => {
    const f = fakeRt()
    S.ensureInstalled(() => f.rt)
    const l = S.sensorsOf(1)
    expect(l.map((v) => [v.name, v.kind, v.sensorNo])).toEqual([['camera', 0, 0], ['thermal', 2, 1], ['mid360', 1, 2]])
    expect(S.hasCamera(1)).toBe(true)
    expect(S.sensorsOf(9).map((v) => v.name)).toEqual(['camera'])
    expect(S.sensorsOf(0)).toEqual([])
    expect(S.hasCamera(0)).toBe(false)
    const cam = l[0]
    expect(cam.w).toBe(6000)
    expect(cam.hfov).toBeCloseTo(Math.PI / 3, 6)
    expect(cam.gimbal.el).toBeCloseTo(-15 * Math.PI / 180, 12)
    expect(cam.rangeM).toBe(300)
    expect(S.sensorsOf(1)).toBe(l) // same array every frame (no allocation)
    expect(S.cache.bytes()).toBeLessThan(64 * 1024)
  })

  it('ingests SensorPose48 raw items and derives the gimbal with the body orientation', () => {
    const f = fakeRt()
    S.ensureInstalled(() => f.rt)
    const cam = S.sensorsOf(1)[0]
    const qb = [0, 0, Math.sin(0.25), Math.cos(0.25)] // yaw 0.5 rad
    // sensor = body · Ry(+30 deg) (gimbal el = -30 deg)
    const h = (30 * Math.PI) / 180 / 2
    const qg = [0, Math.sin(h), 0, Math.cos(h)]
    const qs = [
      qb[3] * qg[0] + qb[0] * qg[3] + qb[1] * qg[2] - qb[2] * qg[1],
      qb[3] * qg[1] - qb[0] * qg[2] + qb[1] * qg[3] + qb[2] * qg[0],
      qb[3] * qg[2] + qb[0] * qg[1] - qb[1] * qg[0] + qb[2] * qg[3],
      qb[3] * qg[3] - qb[0] * qg[0] - qb[1] * qg[1] - qb[2] * qg[2],
    ]
    const got = S.ingestFrame(slot({ swarm: [[1, qb]], swarmT: 1000, full: [[1, 1050, qb]], poses: [[1, 0, 1020, [1, 2, 3], qs, 3], [7, 0, 1020, [0, 0, 0], qs, 3]] }))
    expect(got).toBe(2) // agent 7 gets its views on its first sample (model from the roster)
    expect(cam.valid).toBe(true)
    expect(cam.gimbal.elT / (Math.PI / 180)).toBeCloseTo(-30, 4)
    expect(cam.gimbal.azT).toBeCloseTo(0, 5)
    S.updateGimbals(1000 / 60, 'reduced', 1050)
    expect(cam.gimbal.el).toBeCloseTo(cam.gimbal.elT, 12)
    expect(cam.fovValid).toBe(true)
    S.updateGimbals(16, 'full', 1400)
    expect(cam.fovValid).toBe(false)
  })

  it('subscribes the pose topics of the wanted vehicles and releases them after 1 s', () => {
    const f = fakeRt()
    S.ensureInstalled(() => f.rt)
    const ctx = { nowMs: 0, dtMs: 16, tRenderS: 0 } as unknown as FrameCtx
    S.sensorsOf(2)
    runPhase('world', ctx)
    expect([...f.subs].sort()).toEqual(['uav/v2/sensor/camera/pose', 'uav/v2/sensor/thermal/pose'])
    ;(ctx as { nowMs: number }).nowMs = 1500
    runPhase('world', ctx)
    expect(f.subs.size).toBe(0)
  })

  it('keeps the latest detection per target (sensor.detect)', () => {
    const f = fakeRt()
    S.ensureInstalled(() => f.rt)
    S.clearDetections()
    const base = { type: 'sensor.detect', t_sim_ns: 5e9, seq: 1, level: 1 as const, uav: 'a1', cid: null }
    f.emit({ ...base, data: { target_id: 't1', target_kind: 'person', uav: 'a1', sensor: 'camera', capability: 'rgb.zoom', state: 'suspect', conf: 0.42, pos_enu_m: [1, 2, 0], range_m: 60, pd: 0.51, repeat: false } })
    f.emit({ ...base, seq: 2, data: { target_id: 't1', target_kind: 'person', uav: 'b1', sensor: 'thermal', capability: 'thermal.imaging', state: 'confirmed', conf: 0.9, pos_enu_m: [1, 2, 0], range_m: 63, pd: 0.8, repeat: false } })
    f.emit({ ...base, seq: 3, data: { target_id: 't1', state: 'suspect', conf: 0.9, repeat: true } })
    const l = S.detectionsList()
    expect(l.length).toBe(1)
    expect(l[0].state).toBe('confirmed')
    expect(l[0].count).toBe(3)
    expect(l[0].uav).toBe('a1')
  })
})
