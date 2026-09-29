// P2 composite quad of Tier B/A (M06 §6.5, FR-026, FR-070; g01 §0 item 2 method 1, §6.5). Owner: M06.
// Full-screen quad that samples cloudRT (colour + DepthTexture) inside its sub-viewport (uv x uvScale, clamped half a
// texel inside so no neighbour reads land on cleared pixels), writes the point-cloud depth back (depthNode, AlwaysDepth)
// so the P3 layers depth-test against the cloud, and shades far-plane pixels with the sky Fn (SkyQuad is hidden on
// Tier B/A). M05 owns the EDL composite material (M05 §7.2 edlMaterial with uvScale/strength/taps, bindTargets,
// setBackgroundNode); until it is registered this material is the composite with EDL strength 0 (identical output).
import { AlwaysDepth, DataTexture, DepthTexture, FloatType, Mesh, NeverDepth, OrthographicCamera, PlaneGeometry, Scene, type Texture } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, float, min, positionGeometry, select, texture, uniform, uv, vec2, vec4 } from 'three/tsl'
import { makeSkyUniforms, skyColor, type SkyUniforms } from '../layers/groundSky.materials'

type N = any // TSL nodes

export interface Composite {
  readonly scene: Scene
  readonly camera: OrthographicCamera
  readonly quad: Mesh
  readonly material: MeshBasicNodeMaterial
  readonly sky: SkyUniforms
  readonly uvScale: N
  readonly texel: N
  bindTargets(color: Texture, depth: DepthTexture): void
  dispose(): void
}

export function makeComposite(reversedZ: boolean): Composite {
  const placeholder = new DataTexture(new Uint8Array(4), 1, 1)
  placeholder.needsUpdate = true
  const depthPh = new DepthTexture(1, 1)
  depthPh.type = FloatType
  const uvScale: N = uniform(1)
  const texel: N = uniform(vec2(1, 1))
  const sky = makeSkyUniforms()
  const m = new MeshBasicNodeMaterial()
  const coord: N = Fn(() => {
    const lim: N = vec2(uvScale, uvScale).sub(texel.mul(0.5))
    return min((uv() as N).mul(uvScale), lim)
  })()
  // texture nodes carry the coordinate directly: bindTargets() swaps .value on exactly these nodes
  const colorT: N = texture(placeholder, coord)
  const depthT: N = texture(depthPh, coord)
  // the depth written back is a separate expression (one node read by both colour and depth flows renders black on
  // the classic path, M05 edlComposite note)
  const depthOut: N = texture(depthPh, coord)
  const d: N = depthT.r
  const bg: N = reversedZ ? d.lessThanEqual(1e-7) : d.greaterThanEqual(0.9999999)
  m.colorNode = Fn(() => vec4(select(bg, skyColor(sky, (positionGeometry as N).xy), colorT.rgb) as N, float(1)))() as N
  m.depthNode = depthOut.r
  m.vertexNode = vec4((positionGeometry as N).xy, 0, 1) as N
  m.depthTest = true
  m.depthWrite = true
  // three r186 flips depth functions under a reversed depth buffer (AlwaysDepth would become NEVER): NeverDepth -> GL_ALWAYS
  m.depthFunc = reversedZ ? NeverDepth : AlwaysDepth
  m.fog = false
  const quad = new Mesh(new PlaneGeometry(2, 2), m)
  quad.frustumCulled = false
  quad.name = 'CompositeQuad'
  const scene = new Scene()
  scene.add(quad)
  const camera = new OrthographicCamera(-1, 1, 1, -1, 0, 1)
  return {
    scene, camera, quad, material: m, sky, uvScale, texel,
    bindTargets(color: Texture, depth: DepthTexture): void {
      colorT.value = color
      depthT.value = depth
      depthOut.value = depth
      const img = color.image as { width?: number; height?: number } | undefined
      ;(texel.value as { set(x: number, y: number): void }).set(1 / Math.max(1, img?.width ?? 1), 1 / Math.max(1, img?.height ?? 1))
    },
    dispose(): void {
      quad.geometry.dispose()
      m.dispose()
      placeholder.dispose()
      depthPh.dispose()
    },
  }
}
