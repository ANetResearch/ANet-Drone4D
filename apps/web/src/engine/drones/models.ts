// Low-poly and hero model batches (M06-FR-034, FR-035; AWR-15 §10.4, §10.3 lighting; M06 §6.9). Owner: M06.
// LowPolyBatch: one InstancedMesh per vehicle model with its own material and geometry (M06 §6.3 rules 1 and 11),
// {position, normal, color}, colour = vertex colour x (0.45 + 0.55 max(n.L, 0) + 0.15 (0.5 + 0.5 n.up)) with the same
// EnvLighting sun as the point cloud (uniform, M07 may update it). HeroBatch: InstancedMesh of the hero geometry
// ({position, normal}, g300 matte) and the inverted hull of the red entity (BackSide copy of the hero geometry, pushed
// out along the normals by 2 CSS px worth of world size, r500; count 0 or 1). Only [0, count) of instanceMatrix is
// uploaded. Swapping the hero geometry (glb loaded) also swaps the material so WebGLNodesHandler re-injects the instance
// attributes into the new geometry; the program is the same (no compile).
import { BackSide, BufferGeometry, InstancedMesh, Matrix4, Mesh, Quaternion, Vector3 } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { attribute, dot, float, max, normalLocal, normalWorld, normalize, positionLocal, uniform, vec3, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'

type N = any // TSL nodes

/** default sun in the three frame (E, U, -N): from the south-east, 50 deg high (M07 EnvLighting replaces it) */
export const DEFAULT_SUN = new Vector3(0.45, 0.77, 0.45).normalize()

function lambert(sun: N): N {
  const n: N = normalize(normalWorld)
  return float(0.45).add(float(0.55).mul(max(dot(n, sun), float(0)))).add(float(0.15).mul(n.y.mul(0.5).add(0.5)))
}

export class LowPolyBatch {
  readonly mesh: InstancedMesh
  private readonly sun: N
  count = 0
  constructor(geometry: BufferGeometry, readonly cap: number, name: string) {
    const m = new MeshBasicNodeMaterial()
    this.sun = uniform(DEFAULT_SUN.clone())
    m.colorNode = vec4((attribute('color', 'vec3') as N).mul(lambert(this.sun)), 1) as N
    m.fog = false
    this.mesh = new InstancedMesh(geometry, m, cap)
    this.mesh.count = 0
    this.mesh.frustumCulled = false
    this.mesh.visible = false
    this.mesh.name = name
    this.mesh.instanceMatrix.setUsage(35048) // DynamicDrawUsage
  }
  setSun(dirThree: Vector3): void {
    ;(this.sun.value as Vector3).copy(dirThree)
  }
  setAt(k: number, m: Matrix4): void {
    this.mesh.setMatrixAt(k, m)
  }
  commit(count: number): void {
    this.count = count
    this.mesh.count = count
    this.mesh.visible = count > 0
    if (count > 0) {
      const im = this.mesh.instanceMatrix
      im.clearUpdateRanges()
      im.addUpdateRange(0, count * 16)
      im.needsUpdate = true
    }
  }
  drawCount(): number {
    return this.mesh.visible ? 1 : 0
  }
  dispose(): void {
    this.mesh.geometry.dispose()
    ;(this.mesh.material as MeshBasicNodeMaterial).dispose()
  }
}

export const HULL = { px: 2 } as const

export class HeroBatch {
  readonly mesh: InstancedMesh
  readonly hull: Mesh
  private readonly sun: N
  private readonly hullOffset: N
  private readonly hullColor: N
  count = 0
  hullOn = false
  constructor(geometry: BufferGeometry, readonly cap: number) {
    this.sun = uniform(DEFAULT_SUN.clone())
    this.mesh = new InstancedMesh(geometry, this.makeMaterial(), cap)
    this.mesh.count = 0
    this.mesh.frustumCulled = false
    this.mesh.visible = false
    this.mesh.name = 'DroneHero'
    const hm = new MeshBasicNodeMaterial()
    this.hullOffset = uniform(0.02)
    const r = SCENE.glyphPalette[1]
    this.hullColor = uniform(new Vector3(r[0], r[1], r[2]))
    hm.positionNode = (positionLocal as N).add((normalLocal as N).mul(this.hullOffset)) as N
    hm.colorNode = vec4(this.hullColor, 1) as N
    hm.side = BackSide
    hm.fog = false
    this.hull = new Mesh(geometry.clone(), hm)
    this.hull.frustumCulled = false
    this.hull.visible = false
    this.hull.matrixAutoUpdate = false
    this.hull.name = 'DroneHeroHull'
  }
  private makeMaterial(): MeshBasicNodeMaterial {
    const m = new MeshBasicNodeMaterial()
    const c = SCENE.droneBody
    m.colorNode = vec4(vec3(c[0], c[1], c[2]).mul(lambert(this.sun)), 1) as N
    m.fog = false
    return m
  }
  /** glb loaded: new geometry for the instances and the hull, new material instance (same program) */
  setGeometry(g: BufferGeometry): void {
    const old = this.mesh.geometry
    const oldMat = this.mesh.material as MeshBasicNodeMaterial
    this.mesh.geometry = g
    this.mesh.material = this.makeMaterial()
    old.dispose()
    oldMat.dispose()
    this.hull.geometry.dispose()
    this.hull.geometry = g.clone()
  }
  setSun(dirThree: Vector3): void {
    ;(this.sun.value as Vector3).copy(dirThree)
  }
  setAt(k: number, m: Matrix4): void {
    this.mesh.setMatrixAt(k, m)
  }
  commit(count: number): void {
    this.count = count
    this.mesh.count = count
    this.mesh.visible = count > 0
    if (count > 0) {
      const im = this.mesh.instanceMatrix
      im.clearUpdateRanges()
      im.addUpdateRange(0, count * 16)
      im.needsUpdate = true
    }
  }
  /** red-entity hull at a model matrix (layer frame); offset = 2 CSS px of world size at distance d */
  setHull(on: boolean, m?: Matrix4, worldPerCssPx = 0): void {
    this.hullOn = on
    this.hull.visible = on
    if (!on || !m) return
    this.hull.matrix.copy(m)
    this.hull.matrixWorldNeedsUpdate = true
    this.hullOffset.value = HULL.px * worldPerCssPx
  }
  drawCount(): number {
    return (this.mesh.visible ? 1 : 0) + (this.hull.visible ? 1 : 0)
  }
  dispose(): void {
    this.mesh.geometry.dispose()
    ;(this.mesh.material as MeshBasicNodeMaterial).dispose()
    this.hull.geometry.dispose()
    ;(this.hull.material as MeshBasicNodeMaterial).dispose()
  }
}

/** compose an ENU pose into a layer-frame matrix (the layer sits under WorldRoot, so ENU is the local frame) */
export function poseMatrix(out: Matrix4, p: Vector3, q: Quaternion, s: Vector3): Matrix4 {
  return out.compose(p, q, s)
}
