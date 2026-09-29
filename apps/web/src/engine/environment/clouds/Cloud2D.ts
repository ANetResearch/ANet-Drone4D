// 2D clouds in the sky (M07-FR-037; M07 §6.8.2 item 2; r16 §3.4.4; 15 §10.9). Owner: M07.
// Sky rays (rd_z > 0) hit the cloud plane z = ground + h_mid; the weather-map mask gives alpha = cloud2d_alpha_max m
// smoothstep(0, 0.08, rd_z) in --cloud-2d, and the ray is fogged over at most 20 km. A camera above h_mid never hits the
// plane, so clouds never cut through buildings. Interim drawing (until M06's SkyQuad and P2 background call
// SceneShadingProvider.sky(), M07-to-M06): a full-screen quad at the far plane with depth test on, so on every tier it
// only covers pixels still at the cleared depth (the sky), whatever its position in the draw order; it is transparent
// (an opaque node material forces alpha to 1, g01 §0 item 2) with premultiplied custom blending.
import { BufferGeometry, CustomBlending, Float32BufferAttribute, Mesh, OneFactor, OneMinusSrcAlphaFactor, Sphere, Vector3 } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, cameraPosition, cameraProjectionMatrixInverse, cameraWorldMatrix, exp, float, max, min, normalize, positionGeometry, select, smoothstep, uniform, vec2, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { makeEnvNodes, type EnvNodes, type EnvParams } from '../lighting/EnvUniforms'
import { opticalDepthNode } from '../atmosphere/fogNode'
import { ENV_TIERS } from '../quality/envTiers'
import { cloudMaskNode, type WeatherMap } from './WeatherMap'

type N = any // TSL nodes

/** view ray of a clip-space xy in the three frame */
export function viewDirThree(ndc: N): N {
  const v: N = cameraProjectionMatrixInverse.mul(vec4(ndc, 1, 1))
  return normalize(cameraWorldMatrix.mul(vec4(v.xyz.div(v.w), 0)).xyz)
}

/** premultiplied cloud colour and alpha (vec4) for a three-frame unit direction */
export function cloud2DNode(n: EnvNodes, wm: WeatherMap, dirThree: N): N {
  const rdZ: N = dirThree.y // ENU up
  const camZ: N = cameraPosition.y
  const plane: N = n.groundZ.add(n.cloudHmid)
  const t: N = plane.sub(camZ).div(max(rdZ, float(1e-4)))
  const hit: N = vec2(cameraPosition.x.add(dirThree.x.mul(t)), cameraPosition.z.negate().sub(dirThree.z.mul(t))) // ENU xy
  const m: N = cloudMaskNode(n, wm, hit)
  const up: N = rdZ.greaterThan(0).and(camZ.lessThan(plane))
  const a0: N = n.cloud2DAlphaMax.mul(m).mul(smoothstep(float(0), float(0.08), rdZ)).mul(n.cloudsOn).mul(n.visualOn)
  const L: N = min(t, float(ENV_TIERS.skyFogM))
  const T: N = exp(opticalDepthNode(n, camZ.sub(n.groundZ), rdZ, L).negate())
  const cc: N = uniform(new Vector3(...SCENE.cloud2d))
  const col: N = cc.mul(T).add(n.fogColor.mul(float(1).sub(T)))
  const a: N = select(up, a0, float(0))
  return vec4(col.mul(a), a)
}

export class Cloud2DLayer {
  readonly mesh: Mesh
  readonly nodes: EnvNodes
  private readonly uFar: N

  constructor(p: EnvParams, wm: WeatherMap, reversedZ: boolean) {
    this.nodes = makeEnvNodes(p)
    const n = this.nodes
    this.uFar = uniform(reversedZ ? 1e-6 : 0.999999)
    const m = new MeshBasicNodeMaterial()
    const xy: N = (positionGeometry as N).xy
    m.vertexNode = vec4(xy, this.uFar, 1) as N
    m.colorNode = Fn(() => cloud2DNode(n, wm, viewDirThree(xy)))() as N
    m.transparent = true
    m.blending = CustomBlending
    m.blendSrc = OneFactor
    m.blendDst = OneMinusSrcAlphaFactor
    m.blendSrcAlpha = OneFactor
    m.blendDstAlpha = OneMinusSrcAlphaFactor
    m.depthTest = true
    m.depthWrite = false
    m.fog = false
    const g = new BufferGeometry()
    g.setAttribute('position', new Float32BufferAttribute([-1, -1, 0, 1, -1, 0, 1, 1, 0, -1, -1, 0, 1, 1, 0, -1, 1, 0], 3))
    g.boundingSphere = new Sphere(new Vector3(), 1e9)
    this.mesh = new Mesh(g, m)
    this.mesh.name = 'EnvCloud2D'
    this.mesh.frustumCulled = false
    this.mesh.renderOrder = ENV_TIERS.order.cloud2d
    this.mesh.visible = false
  }

  drawCount(): number {
    return this.mesh.visible ? 1 : 0
  }

  dispose(): void {
    this.mesh.geometry.dispose()
    ;(this.mesh.material as MeshBasicNodeMaterial).dispose()
  }
}
