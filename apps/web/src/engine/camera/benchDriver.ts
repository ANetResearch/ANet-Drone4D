// flight60 camera driver (M06-FR-059, AC-042; AWR-18 §8.6; M05-FR-055). Owner: M06.
// /bench/flight60/<world>.bin: little-endian f32 [3601 x 6] (eye xyz, target xyz, ENU, 60 Hz); <world>.json
// (awr.flight60.v1, snake_case): coordinate_sha256 must equal the sha256 of the world's coordinate.json and bin_sha256
// the sha256 of the .bin, otherwise the driver refuses to run (M06-E012 / PERF-E009). In the camera phase: t = (now -
// t_start) / 1000, k = floor(60 t), linear interpolation between rows k and k + 1 (M05's sampleFlight60, the pure function
// of its Node selector diff), controller input disabled, camera fov/near/far from the json. Every frame
// writes __perf.bench.flightT (frame.t follows through the FrameSampler); t >= 60 s sets __perf.bench.done.
import { checkFlight60, sampleFlight60 } from '../pointcloud/bench/flight60'

export const FLIGHT60 = { fps: 60, frames: 3601, cols: 6, durationS: 60 } as const

export interface Flight60Json {
  schema: string
  world_id: string
  coordinate_sha256: string
  bin_sha256: string
  fps: number
  frames: number
  fov_y_deg: number
  near_m: number
  far_m: number
}

/**
 * eye and target at t seconds (linear between 60 Hz rows); false when t >= 60 s (the last row is written). This is
 * M05's pure function sampleFlight60 (engine/pointcloud/bench/flight60.ts), so the browser camera and the Node selector
 * replay sample the same pose (M05 request 5)
 */
export const flight60Sample: (buf: Float32Array, tSec: number, outEye: Float64Array, outTarget: Float64Array) => boolean = sampleFlight60

export async function sha256Hex(data: ArrayBuffer | Uint8Array): Promise<string> {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data)
  const d = await crypto.subtle.digest('SHA-256', bytes as unknown as ArrayBuffer)
  return Array.from(new Uint8Array(d), (x) => x.toString(16).padStart(2, '0')).join('')
}

export class Flight60Error extends Error {
  constructor(message: string) {
    super(`M06-E012 PERF-E009 ${message}`)
    this.name = 'Flight60Error'
  }
}

export interface Flight60 { json: Flight60Json; buf: Float32Array }

/** load and verify /bench/flight60/<world>.{bin,json} against the world's coordinate.json bytes */
export async function loadFlight60(base: string, worldId: string, coordinateBytes: ArrayBuffer, fetchFn: typeof fetch = fetch): Promise<Flight60> {
  const [jr, br] = await Promise.all([fetchFn(`${base}/bench/flight60/${worldId}.json`), fetchFn(`${base}/bench/flight60/${worldId}.bin`)])
  if (!jr.ok || !br.ok) throw new Flight60Error(`flight60 files of ${worldId} are missing (make flight60)`)
  const json = (await jr.json()) as Flight60Json
  const bin = await br.arrayBuffer()
  return verifyFlight60(json, bin, coordinateBytes)
}

export async function verifyFlight60(json: Flight60Json, bin: ArrayBuffer, coordinateBytes: ArrayBuffer): Promise<Flight60> {
  if (json.schema !== 'awr.flight60.v1') throw new Flight60Error(`unexpected schema ${json.schema}`)
  const bad = checkFlight60(bin, json)
  if (bad) throw new Flight60Error(bad)
  const [binSha, coordSha] = await Promise.all([sha256Hex(bin), sha256Hex(coordinateBytes)])
  if (binSha !== json.bin_sha256) throw new Flight60Error('bin_sha256 does not match the .bin')
  if (coordSha !== json.coordinate_sha256) throw new Flight60Error('coordinate_sha256 does not match the world coordinate.json')
  return { json, buf: new Float32Array(bin) }
}

export class Flight60Driver {
  readonly eye = new Float64Array(3)
  readonly target = new Float64Array(3)
  t0 = Number.NaN
  done = false
  constructor(readonly f: Flight60) {}
  /** camera phase: pose at nowMs (starts on the first call); returns t in seconds */
  step(nowMs: number): number {
    if (Number.isNaN(this.t0)) this.t0 = nowMs
    const t = (nowMs - this.t0) / 1000
    if (!flight60Sample(this.f.buf, t, this.eye, this.target)) this.done = true
    return t
  }
}
