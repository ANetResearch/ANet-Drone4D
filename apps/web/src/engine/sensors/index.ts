// engine/sensors facade (M13 §6.2, §7.4). Owner: M13. M06 discovers this module (viewport/layers/sensors.tsx,
// import.meta.glob) and uses it as its SensorsApi: sensorsOf, hasCamera, frustumCorners, T_base_cam, projectionFor,
// frameRect. The first sensorsOf / hasCamera call installs the runtime side: the world-phase task `m13.gimbal`
// (order 5, before M06's frustums at 10: pending gimbal derivations, critically damped follow, staleness), the
// subscriptions of uav/{id}/sensor/{name}/pose @10 for the vehicles whose sensors were asked for in the last second (the
// gateway then puts them in the interest set), and the sensor.detect event table. SensorPose48 samples arrive through
// ingestFrame(frame) (called by the telemetry phase after RtClient.swapFrame(), M13-to-M06) or onPoseSamples().
// Pure math and TypedArrays only; no react, no render objects, no stores (TS-BND-01).
import { SP48 } from '@awr/contracts/layouts'
import { register, type FrameCtx } from '../loop'
import { rtClient } from '@/net/rt'
import type { RtClient, RtEvent, TelemetryFrame } from '@/net/rt/types'
import { SensorCache, type OrientationSource, type SensorView } from './sensorCache'
import { frameRect as frameRectImpl, frustumCorners as frustumCornersImpl, projectionFor as projectionForImpl, T_base_cam as tBaseCamImpl } from './intrinsics'
import type { SensorSpecJson } from './specs.gen'

export * from './intrinsics'
export * from './gimbalTrack'
export * from './mid360Pattern'
export { SensorCache, KIND, FLAG_ACTIVE, FLAG_FOV_VALID, POSE_HZ, RING, STRIDE, STALE_INTERVALS, type OrientationSource, type SensorView } from './sensorCache'
export { SENSOR_SPECS, type SensorSpecJson, type SensorSpecGimbal } from './specs.gen'

/** raw item schema code of SensorPose48 in the TelemetryFrame (M11 §6.3.8, net/rt/frame.ts RAW_SCHEMA) */
const RAW_POSE48 = 2
const ITEM = 80
const FULL_Q = 16 + 32 // DroneState64.q inside a Full64 item
const WANT_MS = 1000

export const cache = new SensorCache()

/** detection (sensor.detect, M13 §7.2.5) as kept for the GlyphLayer and the sensors store */
export interface Detection {
  targetId: string
  kind: string
  uav: string
  sensor: string
  capability: string
  state: 'suspect' | 'confirmed'
  conf: number
  pos: Float64Array
  rangeM: number
  pd: number
  repeat: boolean
  tSimMs: number
  seq: number
  count: number
}

const detections = new Map<string, Detection>()
let detVersion = 0
const detListeners = new Set<() => void>()

/** latest detection per target (insertion order = first detection) */
export function detectionsList(): readonly Detection[] {
  return [...detections.values()]
}
export function detectionsVersion(): number {
  return detVersion
}
export function onDetections(cb: () => void): () => void {
  detListeners.add(cb)
  return () => detListeners.delete(cb)
}

const str = (x: unknown): string => (typeof x === 'string' ? x : typeof x === 'number' ? String(x) : '')

/** one sensor.detect event (M13 §7.2.5; M13-FR-081) */
export function onDetectEvent(ev: Pick<RtEvent, 'type' | 'data' | 't_sim_ns' | 'seq'> & { uav?: string | null }): void {
  if (ev.type !== 'sensor.detect' || !ev.data) return
  const d = ev.data as Record<string, unknown>
  const id = str(d.target_id)
  if (!id) return
  const p = Array.isArray(d.pos_enu_m) ? (d.pos_enu_m as number[]) : [0, 0, 0]
  const prev = detections.get(id)
  const state = d.state === 'confirmed' ? 'confirmed' : 'suspect'
  const det: Detection = prev ?? {
    targetId: id, kind: '', uav: '', sensor: '', capability: '', state, conf: 0, pos: new Float64Array(3), rangeM: 0,
    pd: 0, repeat: false, tSimMs: 0, seq: 0, count: 0,
  }
  det.kind = str(d.target_kind)
  det.uav = str(d.uav) || str(ev.uav)
  det.sensor = str(d.sensor)
  det.capability = str(d.capability)
  det.state = prev?.state === 'confirmed' ? 'confirmed' : state
  det.conf = Number(d.conf ?? 0)
  det.pos[0] = Number(p[0])
  det.pos[1] = Number(p[1])
  det.pos[2] = Number(p[2])
  det.rangeM = Number(d.range_m ?? 0)
  det.pd = Number(d.pd ?? 0)
  det.repeat = d.repeat === true
  det.tSimMs = Number(ev.t_sim_ns ?? 0) / 1e6
  det.seq = Number(ev.seq ?? 0)
  det.count++
  detections.set(id, det)
  detVersion++
  for (const cb of detListeners) cb()
}

export function clearDetections(): void {
  detections.clear()
  detVersion++
  for (const cb of detListeners) cb()
}

// ------------------------------------------------------------ SensorsApi (M06 engine/drones/frustums.ts)
export function sensorsOf(agentNo: number): readonly SensorView[] {
  ensureInstalled()
  want(agentNo)
  return cache.sensorsOf(agentNo)
}
export function hasCamera(agentNo: number): boolean {
  ensureInstalled()
  want(agentNo)
  return cache.hasCamera(agentNo)
}
export const frustumCorners = frustumCornersImpl as (s: SensorView, L: number, out: Float64Array) => Float64Array
export const T_base_cam = tBaseCamImpl as (s: SensorView, out: Float64Array) => Float64Array
export const projectionFor = projectionForImpl as (s: SensorView, aspect: number, near: number, far: number, out: Float64Array) => Float64Array
export const frameRect = frameRectImpl as (s: SensorView, aspect: number, out: Float64Array) => Float64Array

/** body orientation interpolator (M12 time.interp) when the viewport provides one; else the swarm/Full64 history */
export function setOrientationSource(fn: OrientationSource | null): void {
  cache.orientationSource = fn
}

/** REST sensors{} of a vehicle directory (GET /api/fleet/profiles/{id}), replacing the generated values */
export function setModelSpecs(model: string, specs: readonly SensorSpecJson[]): void {
  cache.setModelSpecs(model, specs)
}

// ------------------------------------------------------------ samples
/**
 * TelemetryFrame of this frame (call right after RtClient.swapFrame()): SensorPose48 raw items, plus the body
 * orientations of the agents with sensor views (swarm rows and Full64 items) for the gimbal derivation.
 */
export function ingestFrame(f: TelemetryFrame): number {
  const sw = f.swarm
  const n = f.hdr.swarmN
  const tS = f.hdr.swarmTSimMs
  for (let i = 0; i < n; i++) {
    const a = sw.agentNo[i]
    cache.pushOrientation(a, tS, sw.quat[4 * i], sw.quat[4 * i + 1], sw.quat[4 * i + 2], sw.quat[4 * i + 3])
  }
  const fd = f.full.bytes
  for (let k = 0; k < f.full.count; k++) {
    const o = f.full.base + ITEM * k
    const a = fd.getUint16(o, true)
    const t = fd.getFloat64(o + 8, true)
    cache.pushOrientation(a, t, fd.getFloat32(o + FULL_Q, true), fd.getFloat32(o + FULL_Q + 4, true),
      fd.getFloat32(o + FULL_Q + 8, true), fd.getFloat32(o + FULL_Q + 12, true))
  }
  let got = 0
  const dv = f.raw.bytes
  for (let k = 0; k < f.raw.count; k++) {
    const o = f.raw.base + ITEM * k
    if (dv.getUint8(o + 4) !== RAW_POSE48 || dv.getUint16(o + 6, true) < SP48.SIZE) continue
    got += poseRecord(dv, o + 16, dv.getFloat64(o + 8, true))
  }
  return got
}

/** one SensorPose48 record at dv[off] with its sample time (simulation ms) */
export function poseRecord(dv: DataView, off: number, tMs: number): number {
  const v = cache.onPose(dv.getUint16(off + SP48.AGENT_NO, true), dv.getUint8(off + SP48.SENSOR_NO), tMs,
    dv.getUint8(off + SP48.FLAGS), dv.getFloat32(off + SP48.POS, true), dv.getFloat32(off + SP48.POS + 4, true),
    dv.getFloat32(off + SP48.POS + 8, true), dv.getFloat32(off + SP48.Q, true), dv.getFloat32(off + SP48.Q + 4, true),
    dv.getFloat32(off + SP48.Q + 8, true), dv.getFloat32(off + SP48.Q + 12, true))
  return v ? 1 : 0
}

/** M13 §7.4 onPoseSamples: n contiguous SensorPose48 rows (48 B each) sampled at tMs */
export function onPoseSamples(rows: DataView, n: number, tMs: number): number {
  let got = 0
  for (let i = 0; i < n; i++) got += poseRecord(rows, i * SP48.SIZE, tMs)
  return got
}

// ------------------------------------------------------------ runtime side (installed on first use)
let installed = false
let offs: (() => void)[] = []
let rtGetter: () => RtClient | null = () => rtClient()
const wantedAt = new Map<number, number>()
const subs = new Map<number, (() => void)[]>()
let nowMs = 0

function want(agentNo: number): void {
  wantedAt.set(agentNo, nowMs)
}

/** world-phase task (M13 §7.4 updateGimbals); exported for tests */
export function updateGimbals(dtMs: number, tier: 'full' | 'lite' | 'reduced', tRenderMs = Number.NaN): void {
  cache.update(dtMs, Number.isFinite(tRenderMs) ? tRenderMs : Number.POSITIVE_INFINITY, tier === 'reduced')
}

function syncSubscriptions(rt: RtClient): void {
  for (const [a, t] of wantedAt) {
    if (nowMs - t > WANT_MS) {
      wantedAt.delete(a)
      const s = subs.get(a)
      if (s) for (const off of s) off()
      subs.delete(a)
      continue
    }
    if (subs.has(a)) continue
    const id = rt.roster.idOf(a)
    if (id === undefined) continue
    const list: (() => void)[] = []
    for (const v of cache.sensorsOf(a)) {
      if (v.kind === 0 || v.kind === 2) list.push(rt.subscribe(`uav/${id}/sensor/${v.name}/pose`, { rate: 10 }))
    }
    subs.set(a, list)
  }
}

function worldTask(ctx: FrameCtx): void {
  nowMs = ctx.nowMs
  const rt = rtGetter()
  if (rt) syncSubscriptions(rt)
  updateGimbals(ctx.dtMs, motionTier(), ctx.tRenderS * 1000)
}

let mql: MediaQueryList | null | undefined
function defaultMotionTier(): 'full' | 'lite' | 'reduced' {
  if (mql === undefined) mql = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null
  return mql?.matches ? 'reduced' : 'full'
}
let motionTier: () => 'full' | 'lite' | 'reduced' = defaultMotionTier

/** motion tier of the page (ui/motion); reduced drops the gimbal damping (M13 §8.2) */
export function setMotionTier(fn: (() => 'full' | 'lite' | 'reduced') | null): void {
  motionTier = fn ?? defaultMotionTier
}

/** installs the world task, the roster model resolver and the event listener (idempotent) */
export function ensureInstalled(getRt: () => RtClient | null = rtGetter): void {
  if (installed) return
  installed = true
  rtGetter = getRt
  cache.modelOf = (a) => rtGetter()?.roster.get(a)?.model || undefined
  offs.push(register('world', 'm13.gimbal', worldTask, { order: 5, layer: 'frustums' }))
  const rt = rtGetter()
  if (rt) {
    offs.push(rt.onEvents((batch) => {
      for (const ev of batch) if (ev.type === 'sensor.detect') onDetectEvent(ev)
    }))
    offs.push(rt.onTime((t) => {
      if (t.epoch !== lastEpoch) {
        if (lastEpoch >= 0) clearDetections()
        lastEpoch = t.epoch
      }
    }))
  }
}
let lastEpoch = -1

/** tests and hot reload: remove the tasks, subscriptions and caches */
export function uninstall(): void {
  for (const off of offs) off()
  offs = []
  for (const s of subs.values()) for (const off of s) off()
  subs.clear()
  wantedAt.clear()
  cache.clear()
  installed = false
  lastEpoch = -1
  rtGetter = () => rtClient()
}
