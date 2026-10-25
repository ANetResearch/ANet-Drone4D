// 公开样例的机队轨迹 `runs/<rid>/tracks_obs.bin`（AWR-17 §18.8.3 `drone4d.bench.tracks.v1`，只有观测层；AWR-14 §21.13 第 4 条：公开站
// 事件回放的机队位置取带噪导航解轨迹）。纯函数，不含 React 与网络。Owner: M15.
// 二进制布局（小端，Content-Type application/x-d3-tracks）：
//   头   "D3TK" | u16 version = 1 | u16 layer（0 obs，1 truth）| u32 header_len | 头 JSON {run_id, hz, t0_ns, n_samples, uavs[]{uav_id, primary_camera}}
//   记录 每机 n_samples × 56 B（按 uavs 顺序）：f64 e, n, u（m）| f32 qx, qy, qz, qw | f32 gimbal_pitch_deg, gimbal_yaw_deg | f32 zoom_x |
//        u8 link_quality（0–3）| u8 flags（bit0 地面，bit1 链路中断）| u16 reserved
//   尾   sha256（32 B）
// 公开构建只接受观测层（layer = 0）：真值层只对评测员（AWR-17 §18.8.3），出现在公开静态目录即视为制品错误，整份拒绝。
// 取样：样本 k 的仿真时刻为 t0_ns + k / hz；任一时刻取相邻两样本的线性插值（位置），区间外取首末样本；航迹折线按步长抽稀。

export const TRACKS_MAGIC = 'D3TK'
export const TRACKS_VERSION = 1
export const TRACKS_RECORD_BYTES = 56
const TRAILER_BYTES = 32
/** 头 JSON 的上限（与解码器的防御性检查一致；3 机的头约 200 B） */
const HEADER_MAX_BYTES = 64 * 1024

export interface UavTrack {
  uavId: string
  primaryCamera: string
  /** ENU 位置（m），每样本 3 个分量 */
  pos: Float64Array
  /** 带噪云台读数（度）：俯仰、偏航 */
  gimbalPitchDeg: Float32Array
  gimbalYawDeg: Float32Array
  linkQuality: Uint8Array
  flags: Uint8Array
}

export interface FleetTracks {
  runId: string
  hz: number
  t0Ns: number
  nSamples: number
  uavs: UavTrack[]
}

export class TracksError extends Error {
  constructor(readonly detail: string) {
    super(`tracks_obs: ${detail}`)
  }
}

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)

/** 解析 tracks.v1（观测层）；布局不符时抛 TracksError */
export function parseTracks(data: ArrayBuffer | Uint8Array): FleetTracks {
  const b = data instanceof Uint8Array ? data : new Uint8Array(data)
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength)
  if (b.byteLength < 12 + TRAILER_BYTES || String.fromCharCode(b[0]!, b[1]!, b[2]!, b[3]!) !== TRACKS_MAGIC) throw new TracksError('magic')
  if (dv.getUint16(4, true) !== TRACKS_VERSION) throw new TracksError(`version ${dv.getUint16(4, true)}`)
  if (dv.getUint16(6, true) !== 0) throw new TracksError('layer is not obs')
  const hl = dv.getUint32(8, true)
  if (hl > HEADER_MAX_BYTES || 12 + hl > b.byteLength) throw new TracksError('header length')
  let head: unknown
  try {
    head = JSON.parse(new TextDecoder().decode(b.subarray(12, 12 + hl)))
  } catch {
    throw new TracksError('header json')
  }
  if (!isObj(head)) throw new TracksError('header json')
  const hz = head.hz
  const n = head.n_samples
  const t0 = head.t0_ns
  const uavs = Array.isArray(head.uavs) ? head.uavs : []
  if (typeof hz !== 'number' || !Number.isInteger(hz) || hz < 1) throw new TracksError('hz')
  if (typeof n !== 'number' || !Number.isInteger(n) || n < 0) throw new TracksError('n_samples')
  if (typeof t0 !== 'number' || !Number.isFinite(t0)) throw new TracksError('t0_ns')
  if (!uavs.length || !uavs.every((u) => isObj(u) && typeof u.uav_id === 'string' && u.uav_id !== '')) throw new TracksError('uavs')
  const body = 12 + hl
  if (b.byteLength !== body + uavs.length * n * TRACKS_RECORD_BYTES + TRAILER_BYTES) throw new TracksError('length')
  const out: UavTrack[] = (uavs as Obj[]).map((u, k) => {
    const tr: UavTrack = {
      uavId: u.uav_id as string,
      primaryCamera: typeof u.primary_camera === 'string' ? u.primary_camera : '',
      pos: new Float64Array(3 * n), gimbalPitchDeg: new Float32Array(n), gimbalYawDeg: new Float32Array(n),
      linkQuality: new Uint8Array(n), flags: new Uint8Array(n),
    }
    for (let i = 0; i < n; i++) {
      const o = body + (k * n + i) * TRACKS_RECORD_BYTES
      tr.pos[3 * i] = dv.getFloat64(o, true)
      tr.pos[3 * i + 1] = dv.getFloat64(o + 8, true)
      tr.pos[3 * i + 2] = dv.getFloat64(o + 16, true)
      tr.gimbalPitchDeg[i] = dv.getFloat32(o + 40, true)
      tr.gimbalYawDeg[i] = dv.getFloat32(o + 44, true)
      tr.linkQuality[i] = dv.getUint8(o + 52)
      tr.flags[i] = dv.getUint8(o + 53)
    }
    return tr
  })
  return { runId: typeof head.run_id === 'string' ? head.run_id : '', hz, t0Ns: t0, nSamples: n, uavs: out }
}

/** 轨迹覆盖的仿真区间（s）；无样本时为 null */
export function tracksSpan(f: FleetTracks): { startS: number; endS: number } | null {
  if (f.nSamples === 0) return null
  return { startS: f.t0Ns / 1e9, endS: f.t0Ns / 1e9 + (f.nSamples - 1) / f.hz }
}

/** 仿真时刻 tS 的平面位置（m，线性插值；区间外取首末样本）；无样本时为 null */
export function trackXY(f: FleetTracks, tr: UavTrack, tS: number): [number, number] | null {
  if (f.nSamples === 0) return null
  const x = (tS - f.t0Ns / 1e9) * f.hz
  if (!(x > 0)) return [tr.pos[0]!, tr.pos[1]!]
  if (x >= f.nSamples - 1) {
    const k = f.nSamples - 1
    return [tr.pos[3 * k]!, tr.pos[3 * k + 1]!]
  }
  const k = Math.floor(x)
  const u = x - k
  return [(1 - u) * tr.pos[3 * k]! + u * tr.pos[3 * k + 3]!, (1 - u) * tr.pos[3 * k + 1]! + u * tr.pos[3 * k + 4]!]
}

/** 航迹折线：每 stepS 秒取一个样本（含末样本），用于平面图的底层航迹 */
export function trackPolyline(f: FleetTracks, tr: UavTrack, stepS = 1): [number, number][] {
  if (f.nSamples === 0) return []
  const step = Math.max(1, Math.round(stepS * f.hz))
  const pts: [number, number][] = []
  for (let k = 0; k < f.nSamples; k += step) pts.push([tr.pos[3 * k]!, tr.pos[3 * k + 1]!])
  const last = f.nSamples - 1
  if (last % step !== 0) pts.push([tr.pos[3 * last]!, tr.pos[3 * last + 1]!])
  return pts
}

/** 测试与夹具用的编码器（与解析器互逆；字段顺序见文件头） */
export function encodeTracks(head: { run_id: string; hz: number; t0_ns: number; uavs: { uav_id: string; primary_camera: string }[] },
  samples: readonly (readonly { e: number; n: number; u: number; flags?: number; link?: number }[])[]): Uint8Array {
  const n = samples[0]?.length ?? 0
  const hj = new TextEncoder().encode(JSON.stringify({ ...head, n_samples: n }))
  const out = new Uint8Array(12 + hj.byteLength + head.uavs.length * n * TRACKS_RECORD_BYTES + TRAILER_BYTES)
  const dv = new DataView(out.buffer)
  out.set(new TextEncoder().encode(TRACKS_MAGIC), 0)
  dv.setUint16(4, TRACKS_VERSION, true)
  dv.setUint16(6, 0, true)
  dv.setUint32(8, hj.byteLength, true)
  out.set(hj, 12)
  head.uavs.forEach((_u, k) => {
    samples[k]!.forEach((s, i) => {
      const o = 12 + hj.byteLength + (k * n + i) * TRACKS_RECORD_BYTES
      dv.setFloat64(o, s.e, true)
      dv.setFloat64(o + 8, s.n, true)
      dv.setFloat64(o + 16, s.u, true)
      dv.setFloat32(o + 36, 1, true) // qw
      dv.setFloat32(o + 48, 1, true) // zoom_x
      dv.setUint8(o + 52, s.link ?? 3)
      dv.setUint8(o + 53, s.flags ?? 0)
    })
  })
  return out
}
