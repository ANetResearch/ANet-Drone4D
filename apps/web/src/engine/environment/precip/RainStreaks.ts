// Low rain: stateless flat-quad streaks (M07-FR-039; M07 §6.8.2 item 4; r16 §3.2.1; 00-index §5.6 E1). Owner: M07.
// Per drop (id = vertexIndex / 6, PCG channels): world tiling p.xy = A + (fract(h + (windOffset - A) / P) - 0.5) P
// (P = 2R, pattern anchored to the world and drifting with the wind), fall fallN = fract(h3 - phase_c / H - A_z / H) with
// one of 4 speed classes (phase_c = fall_rain_m ratio_c mod 960 on the CPU), streak length clamp(|v| 0.042, 0.3, 1.2) m
// along v = (wind, -v_c), width max(D, dist pixelWorldScale 1.15) with opacity D / width (sub-pixel compensation), drop
// diameter from Gamma(4, lambda) clamped to [0.3, 5] mm, near cull 3 m, drops below the DTM culled, alpha times the
// transmittance to the camera, box-edge fade. Live count = floor(cap rain_k) changes drawRange only. Premultiplied alpha,
// no depth write, depth test on, fog off (the transmittance is applied here).
import { CustomBlending, OneFactor, OneMinusSrcAlphaFactor, Vector3, type Mesh } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import {
  Fn, abs, cameraProjectionMatrix, clamp, cross, exp, float, floor, fract, length, log, max, modelViewMatrix, normalize, select, smoothstep,
  uniform, varyingProperty, vec2, vec3, vec4,
} from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { opticalDepthNode } from '../atmosphere/fogNode'
import { makeEnvNodes, type EnvNodes, type EnvParams } from '../lighting/EnvUniforms'
import { ENV_TIERS } from '../quality/envTiers'
import { dtmHeight, makeDtmNodes, type DtmNodes, type EnvTerrain } from '../terrain/dtmSampler'
import { hiddenClip, quadCorner, quadMesh } from './flatQuads'
import { rand } from './nodes/hash'

type N = any // TSL nodes

/** per-box uniforms (a second instance draws the fading octave) */
export interface BoxNodes { anchor: N; R: N; H: N; fade: N }

export function makeBox(): BoxNodes {
  return { anchor: uniform(new Vector3()), R: uniform(20), H: uniform(20), fade: uniform(1) }
}

/** world-tiled drop position of particle iid with fall phase `phase` (m) in a box (ENU) */
export function tiledPosition(n: EnvNodes, b: BoxNodes, iid: N, phase: N): N {
  const P: N = b.R.mul(2)
  const h: N = vec2(rand(iid, 0), rand(iid, 1))
  const xy: N = b.anchor.xy.add(fract(h.add(n.windOffset.xy.sub(b.anchor.xy).div(P))).sub(0.5).mul(P))
  const fallN: N = fract(rand(iid, 2).sub(phase.div(b.H)).sub(b.anchor.z.div(b.H)))
  return vec3(xy, b.anchor.z.add(fallN.sub(0.5).mul(b.H)))
}

export class RainStreaks {
  readonly mesh: Mesh
  readonly nodes: EnvNodes
  readonly box: BoxNodes
  readonly dtm: DtmNodes
  live = 0

  constructor(readonly cap: number, p: EnvParams, terrain: EnvTerrain) {
    this.nodes = makeEnvNodes(p)
    this.box = makeBox()
    this.dtm = makeDtmNodes(terrain)
    const n = this.nodes
    const b = this.box
    const dtm = this.dtm
    const vAlpha = varyingProperty('float', 'vRainAlpha')
    const vU = varyingProperty('float', 'vRainU')
    const m = new MeshBasicNodeMaterial()
    m.vertexNode = Fn(() => {
      const { iid, corner } = quadCorner()
      const cls: N = floor(rand(iid, 3).mul(3.999))
      const ph: N = n.fallPhase4
      const sp: N = n.fallSpeed4
      const phase: N = select(cls.lessThan(0.5), ph.x, select(cls.lessThan(1.5), ph.y, select(cls.lessThan(2.5), ph.z, ph.w)))
      const speed: N = select(cls.lessThan(0.5), sp.x, select(cls.lessThan(1.5), sp.y, select(cls.lessThan(2.5), sp.z, sp.w)))
      const p: N = tiledPosition(n, b, iid, phase).toVar()
      const v: N = vec3(n.precipWind.x, n.precipWind.y, speed.negate())
      const len: N = clamp(length(v).mul(ENV_TIERS.streakExposureS), float(ENV_TIERS.streakMinM), float(ENV_TIERS.streakMaxM))
      const a: N = normalize(v)
      const toCam: N = n.camEnu.sub(p)
      const dist: N = max(length(toCam), float(1e-3))
      const cw: N = cross(a, toCam.div(dist))
      const wdir: N = select(length(cw).greaterThan(1e-4), normalize(cw), vec3(1, 0, 0))
      // Gamma(4, lambda) drop diameter (mm): sum of 4 unit exponentials / lambda
      const g4: N = log(max(rand(iid, 4), float(1e-7))).add(log(max(rand(iid, 5), float(1e-7)))).add(log(max(rand(iid, 6), float(1e-7))))
        .add(log(max(rand(iid, 7), float(1e-7)))).negate()
      const dM: N = clamp(g4.div(n.rainLambda), float(ENV_TIERS.dropMinMm), float(ENV_TIERS.dropMaxMm)).mul(1e-3)
      const width: N = max(dM, dist.mul(n.pixelWorldScale).mul(ENV_TIERS.subpixel))
      const pos: N = p.add(a.mul(corner.y.mul(0.5).mul(len))).add(wdir.mul(corner.x.mul(0.5).mul(width)))
      // transmittance camera -> drop, near fade, box edge fade
      const T: N = exp(opticalDepthNode(n, n.camEnu.z.sub(n.groundZ), toCam.z.negate().div(dist), dist).negate())
      const edge: N = max(abs(p.x.sub(b.anchor.x)), abs(p.y.sub(b.anchor.y))).div(b.R)
      const k: N = float(1).sub(smoothstep(float(0.8), float(1.0), edge))
      vAlpha.assign(dM.div(width).mul(T).mul(k).mul(b.fade).mul(n.visualOn))
      vU.assign(corner.x)
      const below: N = p.z.lessThan(dtmHeight(dtm, p.xy, dtm.tex))
      const cull: N = dist.lessThan(ENV_TIERS.nearCullM).or(below)
      return select(cull, hiddenClip(), cameraProjectionMatrix.mul(modelViewMatrix).mul(vec4(pos, 1)))
    })() as N
    const rgb: N = uniform(new Vector3(...SCENE.precipRain.rgb))
    const baseA = SCENE.precipRain.a
    m.colorNode = Fn(() => {
      const a: N = vAlpha.mul(baseA).mul(float(1).sub(vU.mul(vU)))
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
    this.mesh = quadMesh(m, 'EnvRain', ENV_TIERS.order.rain)
  }

  /** live drops: only the draw range changes */
  setLive(n: number): void {
    this.live = Math.max(0, Math.min(this.cap, n | 0))
    this.mesh.geometry.setDrawRange(0, 6 * this.live)
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
    ;(this.mesh.material as MeshBasicNodeMaterial).dispose()
  }
}
