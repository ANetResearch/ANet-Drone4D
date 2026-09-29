// Attribute-less flat quads for environment particles and glyphs (M07 §6.8.2 items 4, 7; M06 §6.3 rule 10; g01 §3 T6).
// Owner: M07. One draw of 6 vertices per instance: drawRange = 6 n and no position attribute (a fake `position` would
// cap the draw at position.count). vertexIndex / 6 selects the instance, vertexIndex mod 6 the corner of two CCW
// triangles. Particle ids are pre-shuffled by the hash, so every prefix of the draw range is a uniform subset and the
// live count only changes drawRange.
import { BufferGeometry, Mesh, Sphere, Vector3, type Material } from 'three'
import { float, floor, step, vec2, vec4, vertexIndex } from 'three/tsl'

type N = any // TSL nodes

/** instance index (float) and corner in [-1, 1]^2 of the current vertex */
export function quadCorner(): { iid: N; corner: N } {
  const vf: N = float(vertexIndex)
  const iid: N = floor(vf.div(6))
  const c: N = vf.sub(iid.mul(6))
  const cx: N = step(0.5, c).mul(float(1).sub(step(2.5, c))).add(step(3.5, c).mul(float(1).sub(step(4.5, c))))
  const cy: N = step(1.5, c).mul(float(1).sub(step(2.5, c))).add(step(3.5, c))
  return { iid, corner: vec2(cx, cy).mul(2).sub(1) }
}

/** a clip position outside the view volume (culled instances collapse there) */
export const hiddenClip = (): N => vec4(2, 2, 2, 1)

export function quadMesh(material: Material, name: string, renderOrder: number): Mesh {
  const g = new BufferGeometry()
  g.setDrawRange(0, 0)
  g.boundingSphere = new Sphere(new Vector3(), 1e9)
  const m = new Mesh(g, material)
  m.name = name
  m.frustumCulled = false
  m.renderOrder = renderOrder
  m.visible = false
  return m
}
