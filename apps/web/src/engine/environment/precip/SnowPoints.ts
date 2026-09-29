// Low snow and dust: stateless points (M07-FR-040; M07 §6.8.2 item 5; r16 §3.2.2; 15 §10.9). Owner: M07.
// GLPointsNodeMaterial from be.createPointsMaterial() (one vertex per flake: r16 §3.10 measured points cheaper than flat
// quads on SwiftShader), attribute-less (drawRange = n). Snow: 2-3 CSS px in --precip-snow, flutter
// x += sin(phi_snow (1.5 + 2 s) + phi) A with A from 2 m (light snow) to 0.5 m (blizzard) and phi_snow derived from
// fall_snow_m (not time x speed); dust: 1-2 px in --precip-dust, carried by the wind with a slow vertical wobble.
// Positions use the same world tiling as the rain; alpha times the transmittance to the camera; fog off.
import { CustomBlending, OneFactor, OneMinusSrcAlphaFactor, Vector3, type Mesh } from 'three'
import type { PointsNodeMaterial } from 'three/webgpu'
import { BufferGeometry, Points, Sphere } from 'three'
import { Fn, builtin, exp, float, length, max, mix, pointUV, select, sin, uniform, varyingProperty, vec3, vec4, vertexIndex } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { opticalDepthNode } from '../atmosphere/fogNode'
import { makeEnvNodes, type EnvNodes, type EnvParams } from '../lighting/EnvUniforms'
import { ENV_TIERS } from '../quality/envTiers'
import { dtmHeight, makeDtmNodes, type DtmNodes, type EnvTerrain } from '../terrain/dtmSampler'
import { makeBox, tiledPosition, type BoxNodes } from './RainStreaks'
import { rand } from './nodes/hash'

type N = any // TSL nodes

export type PointKind = 'snow' | 'dust'

export class PrecipPoints {
  readonly mesh: Mesh
  readonly nodes: EnvNodes
  readonly box: BoxNodes
  readonly dtm: DtmNodes
  live = 0

  constructor(readonly kind: PointKind, readonly cap: number, p: EnvParams, terrain: EnvTerrain, createPointsMaterial: () => PointsNodeMaterial,
    pointSizeMode: string) {
    this.nodes = makeEnvNodes(p)
    this.box = makeBox()
    this.dtm = makeDtmNodes(terrain)
    const n = this.nodes
    const b = this.box
    const dtm = this.dtm
    const snow = kind === 'snow'
    const tok = snow ? SCENE.precipSnow : SCENE.precipDust
    const px = snow ? ENV_TIERS.snowPx : ENV_TIERS.dustPx
    const vA = varyingProperty('float', snow ? 'vSnowA' : 'vDustA')
    const m = createPointsMaterial()
    m.positionNode = Fn(() => {
      const iid: N = float(vertexIndex)
      const s: N = rand(iid, 3)
      const phi0: N = rand(iid, 4).mul(6.2831853)
      // snow falls by its integral phase; dust hovers (tiny fall) and is carried by windOffset
      const phase: N = snow ? n.snowPhase.mul(mix(float(0.7), float(1.3), s)) : n.snowPhase.mul(0.05)
      const p0: N = tiledPosition(n, b, iid, phase).toVar()
      const amp: N = snow ? mix(float(ENV_TIERS.snowFlutterM[0]), float(ENV_TIERS.snowFlutterM[1]), n.snowK) : float(0.8)
      const w: N = sin(n.snowPhase.mul(float(1.5).add(s.mul(2))).add(phi0)).mul(amp)
      const p: N = vec3(p0.x.add(w), p0.y.add(w.mul(0.6)), p0.z)
      const toCam: N = n.camEnu.sub(p)
      const dist: N = max(length(toCam), float(1e-3))
      const T: N = exp(opticalDepthNode(n, n.camEnu.z.sub(n.groundZ), toCam.z.negate().div(dist), dist).negate())
      const below: N = p.z.lessThan(dtmHeight(dtm, p.xy, dtm.tex))
      vA.assign(select(below.or(dist.lessThan(1.0)), float(0), T.mul(b.fade).mul(n.visualOn)))
      if (pointSizeMode === 'glpoint') builtin('gl_PointSize').assign(mix(float(px[0]), float(px[1]), rand(iid, 5)).mul(n.dpr).max(1.0))
      return p
    })() as N
    const rgb: N = uniform(new Vector3(...tok.rgb))
    const baseA = tok.a
    m.colorNode = Fn(() => {
      const r: N = length((pointUV as N).sub(0.5)).mul(2)
      const a: N = vA.mul(baseA).mul(max(float(1).sub(r.mul(r)), float(0)))
      return vec4(rgb.mul(a), a)
    })() as N
    m.transparent = true
    m.blending = CustomBlending
    m.blendSrc = OneFactor
    m.blendDst = OneMinusSrcAlphaFactor
    m.blendSrcAlpha = OneFactor
    m.blendDstAlpha = OneMinusSrcAlphaFactor
    m.depthWrite = false
    m.depthTest = true
    m.fog = false
    const g = new BufferGeometry()
    g.setDrawRange(0, 0)
    g.boundingSphere = new Sphere(new Vector3(), 1e9)
    const pts = new Points(g, m)
    pts.name = snow ? 'EnvSnow' : 'EnvDust'
    pts.frustumCulled = false
    pts.renderOrder = snow ? ENV_TIERS.order.snow : ENV_TIERS.order.dust
    pts.visible = false
    this.mesh = pts as unknown as Mesh
  }

  setLive(k: number): void {
    this.live = Math.max(0, Math.min(this.cap, k | 0))
    this.mesh.geometry.setDrawRange(0, this.live)
    this.mesh.visible = this.live > 0
  }

  setBox(anchor: ArrayLike<number>, R: number, H: number, fade: number): void {
    ;(this.box.anchor.value as Vector3).set(anchor[0], anchor[1], anchor[2])
    this.box.R.value = R
    this.box.H.value = H
    this.box.fade.value = fade
  }

  drawCount(): number {
    return this.mesh.visible ? 1 : 0
  }

  dispose(): void {
    this.mesh.geometry.dispose()
    ;(this.mesh.material as PointsNodeMaterial).dispose()
  }
}
