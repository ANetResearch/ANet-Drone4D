/** Distance-driven rolling of source wheel groups in ENU (+X forward, +Y axle). */
export interface WheelGroups { ids: Int32Array; centers: Float32Array; radii: Float32Array }
export function rollWheels(base: Float32Array, normals: Float32Array, positionsOut: Float32Array, normalsOut: Float32Array, groups: WheelGroups, distanceM: number): void {
  positionsOut.set(base); normalsOut.set(normals)
  const cosine = Array.from(groups.radii, (radius) => Math.cos(distanceM / radius))
  const sine = Array.from(groups.radii, (radius) => Math.sin(distanceM / radius))
  for (let vertex = 0; vertex < groups.ids.length; vertex++) {
    const wheel = groups.ids[vertex]
    if (wheel < 0 || wheel >= groups.radii.length) continue
    const i = vertex * 3, j = wheel * 3
    const x = base[i] - groups.centers[j], z = base[i + 2] - groups.centers[j + 2]
    positionsOut[i] = groups.centers[j] + cosine[wheel] * x + sine[wheel] * z
    positionsOut[i + 2] = groups.centers[j + 2] - sine[wheel] * x + cosine[wheel] * z
    normalsOut[i] = cosine[wheel] * normals[i] + sine[wheel] * normals[i + 2]
    normalsOut[i + 2] = -sine[wheel] * normals[i] + cosine[wheel] * normals[i + 2]
  }
}
