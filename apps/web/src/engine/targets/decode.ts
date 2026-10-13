// awr.TargetLite32.v1 decoding (M18-FR-018, FR-024; AWR-17 §17.7.3; contract rt/layouts.json). Owner: M18.
// One record per target, n rows back to back in target_no order (topic swarm/target/state, 10 Hz). Rows are decoded into
// preallocated SoA columns (capacity 32, no allocation per frame). Bit fields: state = awareness[0,3] criterion[3]
// moving[4] occupied[5] sound_on[6] enabled[7]; best = level[0,2] captured[2] relay[3] sensor[4,4]; flags = hidden[0]
// discovered_recent[1] cfg_rev mod 64 [2,6].
import { TARGET_LITE32 as L } from '@awr/contracts/layouts'

export const TARGET_CAP = 32
export const AWARENESS = ['UNAWARE', 'SUSPICIOUS', 'REACTING', 'ALERTED', 'CALMING'] as const
export type AwarenessName = (typeof AWARENESS)[number]
export const LEVELS = ['none', 'D', 'R', 'I'] as const
export type LevelName = (typeof LEVELS)[number]
/** PerceptionSensor (rt/enums.json): 0 none, 1 EO, 2 NIR, 3 LWIR, 4 RADAR, 5 ACOUSTIC, 6 LIDAR */
export const PERCEPTION_SENSORS = ['none', 'eo', 'nir', 'lwir', 'radar', 'acoustic', 'lidar'] as const

export interface TargetRows {
  n: number
  agentNo: Uint16Array
  cls: Uint8Array
  aware: Uint8Array
  physical: Uint8Array
  moving: Uint8Array
  occupied: Uint8Array
  soundOn: Uint8Array
  enabled: Uint8Array
  /** ENU m, 3 per row */
  pos: Float64Array
  heading: Float32Array
  speed: Float32Array
  level: Uint8Array
  captured: Uint8Array
  relay: Uint8Array
  sensor: Uint8Array
  alertness: Float32Array
  discovered: Uint16Array
  nVol: Uint8Array
  hidden: Uint8Array
  discoveredRecent: Uint8Array
  cfgRev: Uint8Array
  keepoutR: Float32Array
}

export function newTargetRows(cap = TARGET_CAP): TargetRows {
  return {
    n: 0, agentNo: new Uint16Array(cap), cls: new Uint8Array(cap), aware: new Uint8Array(cap), physical: new Uint8Array(cap),
    moving: new Uint8Array(cap), occupied: new Uint8Array(cap), soundOn: new Uint8Array(cap), enabled: new Uint8Array(cap),
    pos: new Float64Array(cap * 3), heading: new Float32Array(cap), speed: new Float32Array(cap), level: new Uint8Array(cap),
    captured: new Uint8Array(cap), relay: new Uint8Array(cap), sensor: new Uint8Array(cap), alertness: new Float32Array(cap),
    discovered: new Uint16Array(cap), nVol: new Uint8Array(cap), hidden: new Uint8Array(cap), discoveredRecent: new Uint8Array(cap),
    cfgRev: new Uint8Array(cap), keepoutR: new Float32Array(cap),
  }
}

/** decode n rows starting at byte 0 of dv into out (rows beyond the capacity are dropped) */
export function decodeTargetLite32(dv: DataView, n: number, out: TargetRows): void {
  const cap = out.agentNo.length
  const m = Math.min(n, cap, Math.floor(dv.byteLength / L.SIZE))
  for (let i = 0; i < m; i++) {
    const o = i * L.SIZE
    out.agentNo[i] = dv.getUint16(o + L.AGENT_NO, true)
    out.cls[i] = dv.getUint8(o + L.CLASS)
    const st = dv.getUint8(o + L.STATE)
    out.aware[i] = st & 7
    out.physical[i] = (st >> 3) & 1
    out.moving[i] = (st >> 4) & 1
    out.occupied[i] = (st >> 5) & 1
    out.soundOn[i] = (st >> 6) & 1
    out.enabled[i] = (st >> 7) & 1
    out.pos[3 * i] = dv.getFloat32(o + L.POS, true)
    out.pos[3 * i + 1] = dv.getFloat32(o + L.POS + 4, true)
    out.pos[3 * i + 2] = dv.getFloat32(o + L.POS + 8, true)
    out.heading[i] = dv.getFloat32(o + L.HEADING_RAD, true)
    out.speed[i] = dv.getInt16(o + L.SPEED_CMS, true) * 0.01
    const b = dv.getUint8(o + L.BEST)
    out.level[i] = b & 3
    out.captured[i] = (b >> 2) & 1
    out.relay[i] = (b >> 3) & 1
    out.sensor[i] = (b >> 4) & 15
    out.alertness[i] = dv.getUint8(o + L.ALERTNESS_U8) / 255
    out.discovered[i] = dv.getUint16(o + L.DISCOVERED_N, true)
    out.nVol[i] = dv.getUint8(o + L.N_VOL)
    const f = dv.getUint8(o + L.FLAGS)
    out.hidden[i] = f & 1
    out.discoveredRecent[i] = (f >> 1) & 1
    out.cfgRev[i] = (f >> 2) & 63
    out.keepoutR[i] = dv.getFloat32(o + L.KEEPOUT_R_M, true)
  }
  out.n = m
}

/** bytes of a `swarm/target/state` message: raw record bytes (Uint8Array, ArrayBuffer) or the bus object `{rows}` */
export function rowsBytes(data: unknown): Uint8Array | null {
  if (data instanceof Uint8Array) return data
  if (data instanceof ArrayBuffer) return new Uint8Array(data)
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
  if (data && typeof data === 'object' && 'rows' in data) return rowsBytes((data as { rows: unknown }).rows)
  return null
}
