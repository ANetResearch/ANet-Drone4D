// Sensors store (M13-FR-080, FR-081; M13 §8.3, §8.4; AWR-03 §4.3: owner M13, M15 writes the JSX only).
// Fields (read by the drone detail "sensors" tab):
//   vehicleId        primary selection the data belongs to (null when none)
//   sensors[]        every sensor of that vehicle: name, sensorNo, kind, intrinsics (w, h, fx, fy, cx, cy; 0 when none),
//                    hfovDeg x vfovDeg (from the intrinsics, never hard-coded), rangeM, rateHz, conf (A-D), hasGimbal,
//                    state: online | standby | degraded | stale | pending (no sample yet) | planned (LiDAR, V0.2)
//   gimbal           the camera gimbal {yawDeg, pitchDeg, mode, limited} (mode: fixed | look_at | look_at_axis | nadir |
//                    forward, null until state_ext.sens.gimbal arrives); gimbals[] the same per gimballed sensor
//   frameRect        FPV sensor frame inside the viewport as fractions of the viewport (x, y from the top-left, w, h),
//                    null when the aspect ratios match; M15 draws the mask outside it (M13 §8.2)
//   stale, staleS    newest SensorPose48 older than 3 / f (the tab shows STALE <t> S)
//   gnss (ext)       fix name and code, sats, ephM, epvM, hdop, errEnuM (simulated truth error, interest set only), errHM
//   imu (ext)        accMps2, gyroRadS, biasAccMps2, biasGyroRadS (FLU; display conversion only in lib/format.ts)
//   detections (ext) latest sensor.detect per target: targetId, kind, state (suspect | confirmed), conf, uav, capability,
//                    posEnuM, rangeM, count (repeat detections fold into the count), tSimMs
// Writers: while a consumer is mounted (useSensors attaches a reference-counted feed) an overlay task at 4 Hz reads
// engine/sensors (views, smoothed gimbal, staleness) and uav/{id}/state_ext (subscribed at 2 Hz for the primary only), and
// writes only when something changed (<= 4 writes/s, Tier S budget; nothing while the tab is hidden, AWR-14 §4.4).
// Detections are mirrored at <= 4 Hz whenever the event table changes. Text from the wire passes lib/sanitize.ts.
import { useEffect } from 'react'
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { sanitizeText } from '@/lib/sanitize'
import { loop, type FrameCtx } from '@/engine/loop'
import * as sensorsEngine from '@/engine/sensors'
import { rtClient, type DataMsg, type RtClient } from '@/net/rt'
import { selectionStore } from './selection'

export type SensorKindName = 'camera' | 'lidar' | 'thermal' | 'radar' | 'gnss' | 'imu' | 'baro'
export type SensorRowState = 'online' | 'standby' | 'degraded' | 'stale' | 'pending' | 'planned'
export type GimbalModeName = 'fixed' | 'look_at' | 'look_at_axis' | 'nadir' | 'forward'
export type GnssFixName = 'NO_FIX' | 'SINGLE' | 'DGPS' | 'RTK_FLOAT' | 'RTK_FIXED'

export interface SensorInfo {
  name: string
  sensorNo: number
  kind: SensorKindName
  hfovDeg: number | null
  vfovDeg: number | null
  rateHz: number | null
  w: number
  h: number
  fx: number
  fy: number
  cx: number
  cy: number
  rangeM: number | null
  conf: string
  hasGimbal: boolean
  state: SensorRowState
}
export interface GimbalInfo {
  sensorNo: number
  name: string
  yawDeg: number
  pitchDeg: number
  mode: GimbalModeName | null
  limited: boolean
}
export interface GnssInfo {
  fix: GnssFixName | null
  fixCode: number | null
  sats: number | null
  ephM: number | null
  epvM: number | null
  hdop: number | null
  errEnuM: [number, number, number] | null
  errHM: number | null
}
export interface ImuInfo {
  accMps2: [number, number, number]
  gyroRadS: [number, number, number]
  biasAccMps2: [number, number, number]
  biasGyroRadS: [number, number, number]
}
export interface DetectionRow {
  targetId: string
  kind: string
  state: 'suspect' | 'confirmed'
  conf: number
  uav: string
  sensor: string
  capability: string
  posEnuM: [number, number, number]
  rangeM: number
  count: number
  tSimMs: number
}
export interface SensorsState {
  vehicleId: string | null
  sensors: readonly SensorInfo[]
  gimbal: { pitchDeg: number; yawDeg: number; mode: GimbalModeName | null; limited: boolean } | null
  gimbals: readonly GimbalInfo[]
  frameRect: { x: number; y: number; w: number; h: number } | null
  stale: boolean
  staleS: number | null
  gnss: GnssInfo | null
  imu: ImuInfo | null
  detections: readonly DetectionRow[]
  version: number
}

export const sensorsStore = createAwrStore<SensorsState>('sensors', () => ({
  vehicleId: null, sensors: [], gimbal: null, gimbals: [], frameRect: null, stale: false, staleS: null, gnss: null, imu: null,
  detections: [], version: 0,
}))

export const FIX_NAMES: readonly GnssFixName[] = ['NO_FIX', 'SINGLE', 'DGPS', 'RTK_FLOAT', 'RTK_FIXED']
const KIND_NAMES: Record<number, SensorKindName> = { 0: 'camera', 1: 'lidar', 2: 'thermal', 3: 'radar', 4: 'gnss', 5: 'imu', 6: 'baro' }
const MODE_NAMES: readonly GimbalModeName[] = ['fixed', 'look_at', 'look_at_axis', 'nadir', 'forward']
const R2D = 180 / Math.PI
export const SENSORS_STORE_HZ = 4

// ------------------------------------------------------------ pure builders (tested without a page)
function num(x: unknown): number | null {
  return typeof x === 'number' && Number.isFinite(x) ? x : null
}
function vec3(x: unknown): [number, number, number] | null {
  return Array.isArray(x) && x.length === 3 && x.every((v) => typeof v === 'number' && Number.isFinite(v))
    ? [x[0] as number, x[1] as number, x[2] as number]
    : null
}

/** state_ext.loc (M13 §7.2.3) -> GnssInfo; null when the vehicle reports no GNSS */
export function gnssFromExt(ext: Record<string, unknown> | null | undefined): GnssInfo | null {
  const loc = ext?.loc as Record<string, unknown> | undefined
  if (!loc || loc.gnss_fix === null || loc.gnss_fix === undefined) return null
  const code = num(loc.gnss_fix)
  const err = vec3(loc.err_enu_m)
  return {
    fix: code !== null && code >= 0 && code < FIX_NAMES.length ? FIX_NAMES[code] : null, fixCode: code, sats: num(loc.sats),
    ephM: num(loc.eph_m), epvM: num(loc.epv_m), hdop: num(loc.hdop), errEnuM: err, errHM: err ? Math.hypot(err[0], err[1]) : null,
  }
}

/** state_ext.sens.imu -> ImuInfo */
export function imuFromExt(ext: Record<string, unknown> | null | undefined): ImuInfo | null {
  const imu = (ext?.sens as Record<string, unknown> | undefined)?.imu as Record<string, unknown> | undefined
  if (!imu) return null
  const a = vec3(imu.acc_mps2)
  const g = vec3(imu.gyro_rad_s)
  const ba = vec3(imu.bias_acc_mps2)
  const bg = vec3(imu.bias_gyro_rad_s)
  return a && g && ba && bg ? { accMps2: a, gyroRadS: g, biasAccMps2: ba, biasGyroRadS: bg } : null
}

/** state_ext.sens.gimbal[] -> mode and limit per sensor_no */
export function gimbalModesFromExt(ext: Record<string, unknown> | null | undefined): Map<number, { mode: GimbalModeName | null; limited: boolean }> {
  const out = new Map<number, { mode: GimbalModeName | null; limited: boolean }>()
  const rows = (ext?.sens as Record<string, unknown> | undefined)?.gimbal
  if (!Array.isArray(rows)) return out
  for (const r of rows as Record<string, unknown>[]) {
    const no = num(r.sensor_no)
    if (no === null) continue
    const m = typeof r.mode === 'string' && (MODE_NAMES as readonly string[]).includes(r.mode) ? (r.mode as GimbalModeName) : null
    out.set(no, { mode: m, limited: r.limited === true })
  }
  return out
}

/** sensor rows of an agent from engine/sensors views plus the non-FOV sensors of its vehicle directory */
export function buildSensorRows(model: string, views: readonly sensorsEngine.SensorView[]): SensorInfo[] {
  const specs = sensorsEngine.cache.specsFor(model)
  const out: SensorInfo[] = []
  for (const sp of specs) {
    const v = views.find((x) => x.sensorNo === sp.sensorNo)
    const kind = KIND_NAMES[sp.kindCode] ?? 'camera'
    let state: SensorRowState = 'online'
    if (kind === 'lidar') state = 'planned'
    else if (v && v.valid) state = !v.active ? 'standby' : !v.fovValid ? 'stale' : 'online'
    else if (v) state = 'pending'
    out.push({
      name: sanitizeText(sp.name, 32), sensorNo: sp.sensorNo, kind,
      hfovDeg: sp.hfov !== null && sp.w > 0 ? sp.hfov * R2D : kind === 'lidar' && sp.hfov !== null ? sp.hfov * R2D : null,
      vfovDeg: sp.vfov !== null && (sp.w > 0 || kind === 'lidar') ? sp.vfov * R2D : null,
      rateHz: sp.rateHz, w: sp.w, h: sp.h, fx: sp.fx, fy: sp.fy, cx: sp.cx, cy: sp.cy, rangeM: sp.rangeM,
      conf: sp.conf ?? '', hasGimbal: sp.gimbal !== null, state,
    })
  }
  return out
}

/** FPV frame rect as fractions of the viewport (M13-FR-023); null when the aspect ratios match */
export function frameRectFraction(v: sensorsEngine.SensorView, aspect: number): SensorsState['frameRect'] {
  if (!(v.w > 0 && v.h > 0 && aspect > 0)) return null
  const r = sensorsEngine.frameRect(v, aspect, new Float64Array(4))
  if (r[0] <= -1 && r[1] <= -1) return null
  return { x: (r[0] + 1) / 2, y: (1 - r[3]) / 2, w: (r[2] - r[0]) / 2, h: (r[3] - r[1]) / 2 }
}

export function detectionRows(list: readonly sensorsEngine.Detection[]): DetectionRow[] {
  return list.map((d) => ({
    targetId: sanitizeText(d.targetId, 32), kind: sanitizeText(d.kind, 32), state: d.state, conf: d.conf, uav: sanitizeText(d.uav, 64),
    sensor: sanitizeText(d.sensor, 32), capability: sanitizeText(d.capability, 64), posEnuM: [d.pos[0], d.pos[1], d.pos[2]],
    rangeM: d.rangeM, count: d.count, tSimMs: d.tSimMs,
  }))
}

// ------------------------------------------------------------ feed
let refs = 0
let offs: (() => void)[] = []
let extSub: { id: string; off: () => void } | null = null
let lastExt: Record<string, unknown> | null = null
let lastExtId: string | null = null
let lastKey = ''
let detSeen = -1

function onData(m: DataMsg): void {
  if (!m.topic.endsWith('/state_ext') || !m.data || typeof m.data !== 'object') return
  const id = m.topic.split('/')[1]
  if (id !== selectionStore.getState().primary) return
  lastExt = m.data as Record<string, unknown>
  lastExtId = id
}

function tick(ctx: FrameCtx | null, rt: RtClient | null): void {
  const primary = selectionStore.getState().primary
  if (rt && extSub?.id !== primary) {
    extSub?.off()
    extSub = primary ? { id: primary, off: rt.subscribe(`uav/${primary}/state_ext`, { rate: 2 }) } : null
    lastExt = null
    lastExtId = null
  }
  const s = sensorsStore.getState()
  const patch: Partial<SensorsState> = {}
  if (!primary || !rt) {
    if (s.vehicleId !== null) Object.assign(patch, { vehicleId: null, sensors: [], gimbal: null, gimbals: [], frameRect: null, gnss: null, imu: null })
  } else {
    const a = rt.roster.agentNoOf(primary)
    const model = a >= 0 ? rt.roster.get(a)?.model : undefined
    const views = a >= 0 ? sensorsEngine.sensorsOf(a) : []
    const ext = lastExtId === primary ? lastExt : null
    const modes = gimbalModesFromExt(ext)
    const rows = model ? buildSensorRows(model, views) : []
    const gimbals: GimbalInfo[] = []
    let newest = Number.NaN
    for (const v of views) {
      if (Number.isFinite(v.poseAgeS)) newest = Number.isFinite(newest) ? Math.min(newest, v.poseAgeS) : v.poseAgeS
      if (!v.hasGimbal) continue
      const m = modes.get(v.sensorNo)
      gimbals.push({ sensorNo: v.sensorNo, name: v.name, yawDeg: v.gimbal.azT * R2D, pitchDeg: v.gimbal.elT * R2D, mode: m?.mode ?? null, limited: m?.limited ?? false })
    }
    const cam = views.find((v) => v.kind === 0)
    const g0 = gimbals.find((g) => cam && g.sensorNo === cam.sensorNo) ?? null
    const aspect = ctx && ctx.cssH > 0 ? ctx.cssW / ctx.cssH : 16 / 9
    const staleS = Number.isFinite(newest) ? newest : null
    Object.assign(patch, {
      vehicleId: primary, sensors: rows, gimbals, gimbal: g0 ? { pitchDeg: g0.pitchDeg, yawDeg: g0.yawDeg, mode: g0.mode, limited: g0.limited } : null,
      frameRect: cam ? frameRectFraction(cam, aspect) : null, stale: staleS !== null && staleS > 0.3, staleS,
      gnss: gnssFromExt(ext), imu: imuFromExt(ext),
    })
  }
  const dv = sensorsEngine.detectionsVersion()
  if (dv !== detSeen) {
    detSeen = dv
    patch.detections = detectionRows(sensorsEngine.detectionsList())
  }
  const key = JSON.stringify(patch)
  if (key === lastKey || key === '{}') return
  lastKey = key
  sensorsStore.setState({ ...patch, version: s.version + 1 })
}

/** reference-counted feed (useSensors attaches it while mounted); returns the detach function */
export function attachSensorsFeed(getRt: () => RtClient | null = rtClient): () => void {
  refs++
  if (refs === 1) {
    const rt = getRt()
    if (rt) sensorsEngine.ensureInstalled(getRt)
    offs = [
      loop.register('overlay', 'm13.sensors-store', (ctx) => tick(ctx, getRt()), { fps: SENSORS_STORE_HZ, order: 20 }),
    ]
    if (rt) offs.push(rt.onData(onData))
  }
  let done = false
  return () => {
    if (done) return
    done = true
    refs--
    if (refs === 0) {
      for (const off of offs) off()
      offs = []
      extSub?.off()
      extSub = null
      lastExt = null
      lastKey = ''
    }
  }
}

/** test helper: one feed tick without the frame loop */
export function sensorsFeedTick(ctx: FrameCtx | null, rt: RtClient | null): void {
  tick(ctx, rt)
}

// detections are needed by the event table even when the sensors tab is hidden: mirror them at <= 4 Hz once the page
// RtClient exists (a cheap version check per tick)
loop.register('overlay', 'm13.sensors-store.detections', () => {
  const rt = rtClient()
  if (!rt) return
  sensorsEngine.ensureInstalled()
  const dv = sensorsEngine.detectionsVersion()
  if (dv === detSeen || refs > 0) return
  detSeen = dv
  sensorsStore.setState({ detections: detectionRows(sensorsEngine.detectionsList()), version: sensorsStore.getState().version + 1 })
}, { fps: SENSORS_STORE_HZ, order: 21 })

/** React hook: selector over the sensors store; mounting it attaches the feed (only visible tabs drive updates) */
export function useSensors<T>(sel: (s: SensorsState) => T): T {
  useEffect(() => attachSensorsFeed(), [])
  return useStore(sensorsStore, sel)
}

/** detections only (event table, overlay legend): does not attach the per-vehicle feed */
export function useDetections<T>(sel: (d: readonly DetectionRow[]) => T): T {
  return useStore(sensorsStore, (s) => sel(s.detections))
}
