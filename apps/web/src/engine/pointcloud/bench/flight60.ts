// flight60 sampling (M05-FR-055; AWR-18 §8.6(4); M06-FR-059). Owner: M05. Pure function used by M06's camera-phase
// bench driver and by the Node-side replays: t in seconds, k = floor(t x 60), linear interpolation between rows k and
// k + 1 of the f32 [3601 x 6] table (eye xyz, target xyz, ENU). Returns false for t >= 60 s (the flight is over).
export const FLIGHT60_FPS = 60
export const FLIGHT60_FRAMES = 3601
export const FLIGHT60_BYTES = FLIGHT60_FRAMES * 6 * 4

export function sampleFlight60(buf: Float32Array, tSec: number, outEye: Float64Array, outTarget: Float64Array): boolean {
  if (!(tSec < 60)) {
    const o = 6 * (FLIGHT60_FRAMES - 1)
    for (let a = 0; a < 3; a++) {
      outEye[a] = buf[o + a]
      outTarget[a] = buf[o + 3 + a]
    }
    return false
  }
  const x = Math.max(0, tSec) * FLIGHT60_FPS
  const k = Math.min(Math.floor(x), FLIGHT60_FRAMES - 2)
  const f = x - k
  const o0 = 6 * k
  const o1 = o0 + 6
  for (let a = 0; a < 3; a++) {
    outEye[a] = buf[o0 + a] + (buf[o1 + a] - buf[o0 + a]) * f
    outTarget[a] = buf[o0 + 3 + a] + (buf[o1 + 3 + a] - buf[o0 + 3 + a]) * f
  }
  return true
}

/** checks of a loaded flight: size and the awr.flight60.v1 companion hashes (PERF-E009 when they disagree) */
export function checkFlight60(bin: ArrayBuffer, json: { schema?: string; frames?: number; fps?: number }): string | null {
  if (bin.byteLength !== FLIGHT60_BYTES) return `flight60 .bin is ${bin.byteLength} B, expected ${FLIGHT60_BYTES}`
  if (json.schema !== 'awr.flight60.v1' || json.frames !== FLIGHT60_FRAMES || json.fps !== FLIGHT60_FPS) return 'flight60 .json is not awr.flight60.v1'
  return null
}
