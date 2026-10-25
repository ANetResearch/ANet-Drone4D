/** Perspective fit for a centred box, ENU +Z up. Long vehicles should use panel width, not a sphere's diameter. */
export function previewDistance(size: readonly number[], yaw: number, pitch: number, aspect: number, fovDeg = 38): number {
  const cp = Math.cos(pitch), sp = Math.sin(pitch), cy = Math.cos(yaw), sy = Math.sin(yaw)
  const vertical = Math.tan(fovDeg * Math.PI / 360), horizontal = vertical * aspect
  let distance = 0
  for (const x of [-size[0] / 2, size[0] / 2]) for (const y of [-size[1] / 2, size[1] / 2]) for (const z of [-size[2] / 2, size[2] / 2]) {
    const depth = x * cy * cp + y * sy * cp + z * sp
    const right = -x * sy + y * cy, up = -x * cy * sp - y * sy * sp + z * cp
    distance = Math.max(distance, depth + Math.abs(right) / horizontal, depth + Math.abs(up) / vertical)
  }
  return Math.max(.1, distance) * 1.12
}
