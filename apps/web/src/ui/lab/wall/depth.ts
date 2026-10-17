// Depth tile rendering (AWR-14 §21.7.3): planar depth in metres to a grey ramp on a log scale, 0.3 m white to 60 m black;
// no geometry (0), non-finite or out of range draws the g900 neutral (lieflat gray, AWR-15 §3.1).
export const DEPTH_NEAR_M = 0.3
export const DEPTH_FAR_M = 60
const G900 = 24

export function depthToRgba(depth: Float32Array, out?: Uint8ClampedArray<ArrayBuffer>): Uint8ClampedArray<ArrayBuffer> {
  const px = out ?? new Uint8ClampedArray(depth.length * 4)
  const l0 = Math.log(DEPTH_NEAR_M)
  const span = Math.log(DEPTH_FAR_M) - l0
  for (let i = 0; i < depth.length; i++) {
    const d = depth[i]!
    let g: number
    if (!(d > 0) || !Number.isFinite(d)) g = G900
    else if (d <= DEPTH_NEAR_M) g = 255
    else if (d >= DEPTH_FAR_M) g = 0
    else g = Math.round(255 * (1 - (Math.log(d) - l0) / span))
    const o = i * 4
    px[o] = g
    px[o + 1] = g
    px[o + 2] = g
    px[o + 3] = 255
  }
  return px
}
