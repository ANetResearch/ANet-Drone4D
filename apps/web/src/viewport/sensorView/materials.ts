// Sensor view material, TSL (D2 WP-11; AWR-04 §6.1, §6.2, §10.5, §10.7, ADR-108; AWR-14 §20.10.5). Owner: M06.
// One full-screen composite material holding the three shading variants (a uniform selects the branch: one program,
// built once and warmed in the shader zoo, so switching views never compiles, D2-AC-21, and the zoo pays for one program
// instead of three): it reads the frame the world layers rendered into the sensor target (colour as the canvas would
// show it, see pass.ts, and the depth texture) and draws it to the canvas with the depth written back, so the overlays of
// the EO view still depth-test against the scene.
//   EO (day)     the frame graded by the passive light level: gain and saturation from M19's k_light curves (colour
//                0.5 lx, mono 0.05 lx), luma noise at low light, a slight vignette;
//   NIR (night)  monochrome through the neutral token ramp: passive low light (0.05 lx curve) plus the active 850 nm
//                illumination of up to two illuminators coaxial with the camera (cone of the divergence, 1/R^2 from
//                0.5 R_nir, M19's edge band clamp((R_nir - R) / (0.2 R_nir))), noise rising as the light falls;
//   LWIR         a temperature per pixel: the background surface temperature of the calendar (M07) varied by albedo,
//                height above the ground (structures keep heat) and sun-facing orientation by day (normals from the
//                reconstructed world position: "DSM and calendar"), the sky temperature beyond the far plane, path
//                transmittance towards the air temperature,
//                then the emitters (targets by class temperature and emissivity, other vehicles' motors) as rounded
//                silhouettes of their projected boxes, occluded by nearer surfaces; automatic gain range, white-hot or
//                black-hot through the same neutral ramp (no hue: no red pixel, D2-AC-25); 1-4x digital zoom.
// The material owns its uniform nodes (never shared between materials under WebGLNodesHandler, viewport/README).
// The world position comes from the depth (reversed or standard) through the camera's projection inverse and world
// matrix, written per frame by pass.ts (the same pattern as the Tier S SkyQuad). Pixels left at the cleared depth are the
// sky or the ground grid (which tests depth but never writes it): a ray below the horizon is intersected with the grid's
// plane, so the ground has a range and a temperature of its own. Colours: the scene tokens only.
import { AlwaysDepth, DataTexture, DepthStencilFormat, DepthTexture, FloatType, Matrix4, NearestFilter, NeverDepth, RGBAFormat, UnsignedInt248Type, Vector2, Vector3, type Texture } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import {
  Break, Fn, If, Loop, abs, clamp, cross, dFdx, dFdy, dot, exp, float, fract, int, ivec2, length, max, min, mix, normalize, positionGeometry, pow,
  sRGBTransferEOTF, select, sin, smoothstep, texture, uniform, uv, vec2, vec3, vec4,
} from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { SV } from './math'

type N = any // TSL nodes

const LUMA = [0.2126, 0.7152, 0.0722] as const

/** emitter records: SV.maxEmitters rows of 3 RGBA32F texels (see pass.ts writeEmitters) */
export const EMIT_COLS = 3

export interface SvUniforms {
  /** 0 EO, 1 night (NIR), 2 LWIR */
  mode: N
  projInv: N
  camWorld: N
  camPos: N
  res: N
  seed: N
  zoom: N
  /** ground plane height in the three frame (y up = ENU z): the ground grid's plane */
  groundY: N
  // EO: gain, saturation, noise
  gain: N
  sat: N
  noise: N
  // NIR: passive factor; ill0, ill1 = (on 0/1, cos of the inner half angle, cos of the outer half angle); r0, r1 = R_nir (m)
  kPas: N
  ill0: N
  ill1: N
  r0: N
  r1: N
  nirGain: N
  // LWIR: temperatures (deg C), emissivity, extinction (1/m), day factor, sun (three frame), gain range, palette, emitters
  tAir: N
  tSurf: N
  tSky: N
  epsBg: N
  sigma: N
  day: N
  sun: N
  lo: N
  hi: N
  blackHot: N
  count: N
  albK: N
  sunK: N
  structK: N
}

export const MODE_INDEX = { eo: 0, nir: 1, lwir: 2 } as const

export interface SensorViewMaterial {
  readonly material: MeshBasicNodeMaterial
  readonly u: SvUniforms
  bindTargets(color: Texture, depth: DepthTexture): void
  dispose(): void
}

function makeUniforms(): SvUniforms {
  return {
    mode: uniform(0), projInv: uniform(new Matrix4()), camWorld: uniform(new Matrix4()), camPos: uniform(new Vector3()), res: uniform(new Vector2(1, 1)),
    seed: uniform(0), zoom: uniform(1), groundY: uniform(0),
    gain: uniform(1), sat: uniform(1), noise: uniform(0),
    kPas: uniform(0), ill0: uniform(new Vector3(0, 1, 1)), ill1: uniform(new Vector3(0, 1, 1)), r0: uniform(0), r1: uniform(0), nirGain: uniform(1.6),
    tAir: uniform(15), tSurf: uniform(18), tSky: uniform(-20), epsBg: uniform(0.95), sigma: uniform(2e-4), day: uniform(1),
    sun: uniform(new Vector3(0.3, 0.77, 0.55)), lo: uniform(10), hi: uniform(30), blackHot: uniform(0), count: uniform(0),
    albK: uniform(6), sunK: uniform(3), structK: uniform(2),
  }
}

/** the neutral token ramp (pcRamp, linear sRGB) at t in [0, 1] */
function ramp(t: N): N {
  const R = SCENE.pcRamp
  const x: N = clamp(t, 0, 1).mul(R.length - 1)
  let c: N = vec3(R[0][0], R[0][1], R[0][2])
  for (let i = 1; i < R.length; i++) c = mix(c, vec3(R[i][0], R[i][1], R[i][2]), clamp(x.sub(i - 1), 0, 1))
  return c
}

/** per-pixel hash in [0, 1) (screen px and frame seed) */
function hash(px: N, seed: N): N {
  return fract(sin(dot(px.add(vec2(seed.mul(17.0), seed.mul(59.0))), vec2(12.9898, 78.233))).mul(43758.5453))
}

const sdRound = (p: N, half: N, r: N): N => {
  const q: N = abs(p).sub(half.sub(r))
  return length(max(q, vec2(0, 0))).add(min(max(q.x, q.y), 0)).sub(r)
}

/**
 * the sensor view material: shared front end (sampling coordinate with the LWIR digital zoom about the centre, v
 * pre-flipped like the P2 composite; the colour as the canvas shows it, decoded to linear; the depth; view and world
 * positions with the ground plane for cleared pixels; range), then the branch of the current view
 */
export function makeSensorViewMaterial(reversedZ: boolean, emitters: DataTexture): SensorViewMaterial {
  const u = makeUniforms()
  const colorPh = new DataTexture(new Uint8Array(4), 1, 1)
  colorPh.needsUpdate = true
  // the placeholder has the sensor target's depth format (pass.ts: the canvas's packed depth-stencil)
  const depthPh = new DepthTexture(1, 1, UnsignedInt248Type)
  depthPh.format = DepthStencilFormat
  const uz: N = (uv() as N).sub(0.5).div(u.zoom).add(0.5)
  const st: N = vec2(uz.x, float(1).sub(uz.y))
  const colorT: N = texture(colorPh, st)
  const depthT: N = texture(depthPh, st)
  // the written-back depth is its own texture node (one node read by colour and depth renders black, composite.ts)
  const depthOut: N = texture(depthPh, st)
  const em: N = texture(emitters)
  const m = new MeshBasicNodeMaterial()
  m.name = 'SensorView'
  m.colorNode = Fn(() => {
    // the shared values are variables of the main scope (toVar): a plain node is emitted where it is first used, which is
    // inside the first branch, and the other branches would read it unassigned
    const lin: N = (sRGBTransferEOTF(colorT.rgb) as N).toVar()
    const Y: N = dot(lin, vec3(LUMA[0], LUMA[1], LUMA[2])).toVar()
    const d: N = depthT.r.toVar()
    const zN: N = reversedZ ? d : d.mul(2).sub(1)
    const ndc: N = uz.mul(2).sub(1).toVar()
    const v4: N = u.projInv.mul(vec4(ndc, zN, 1))
    const view0: N = v4.xyz.div(v4.w)
    const far: N = reversedZ ? d.lessThanEqual(1e-7) : d.greaterThanEqual(0.9999999)
    // cleared depth: the ground plane when the ray points below the horizon and meets it in front, else the sky
    const world0: N = u.camWorld.mul(vec4(view0, 1)).xyz
    const dir: N = normalize(world0.sub(u.camPos))
    const tG: N = u.groundY.sub(u.camPos.y).div(min(dir.y, -1e-4))
    const ground: N = far.and(dir.y.lessThan(-1e-4)).and(tG.greaterThan(0))
    const sky: N = far.and(ground.not()).toVar()
    const view: N = select(ground, normalize(view0).mul(tG), view0).toVar()
    const world: N = select(ground, u.camPos.add(dir.mul(tG)), world0).toVar()
    const dist: N = length(view).toVar()
    const px: N = (uv() as N).mul(u.res).toVar()
    const out: N = vec3(0, 0, 0).toVar()
    If(u.mode.lessThan(0.5), () => {
      // EO: the frame graded by the passive light level; noise grows with the gain (shot noise plus a read-noise floor)
      const c: N = mix(vec3(Y, Y, Y), lin, u.sat).mul(u.gain)
      const n: N = hash(px, u.seed).sub(0.5).mul(u.noise)
      const vig: N = float(1).sub(dot(ndc, ndc).mul(0.09))
      out.assign(max(c.mul(n.mul(2).add(1)).add(vec3(n, n, n).mul(0.03)), vec3(0, 0, 0)).mul(vig))
    }).ElseIf(u.mode.lessThan(1.5), () => {
      // NIR: passive mono plus the illuminators coaxial with the camera
      const active = (ill: N, rNir: N, cosA: N): N => {
        const cone: N = smoothstep(ill.z, ill.y, cosA)
        const edge: N = clamp(rNir.sub(dist).div(max(rNir.mul(SV.nirEdgeFrac), 1e-3)), 0, 1)
        const ref: N = rNir.mul(SV.nirRefFrac)
        const fall: N = min(ref.mul(ref).div(max(dist.mul(dist), 1)), SV.nirGainMax)
        return ill.x.mul(cone).mul(edge).mul(fall)
      }
      const cosA: N = view.z.negate().div(max(dist, 1e-3))
      const act: N = select(sky, float(0), max(active(u.ill0, u.r0, cosA), active(u.ill1, u.r1, cosA)))
      const light: N = u.kPas.add(act)
      // near-infrared reflectance is higher than the visible luma for most surfaces (vegetation brightest): lift the mid tones
      const refl: N = pow(max(Y, 1e-4), float(0.7))
      const v: N = refl.mul(light).mul(select(sky, float(0.5), float(1))).mul(u.nirGain)
      const sigma: N = float(0.02).add(float(0.08).mul(float(1).sub(clamp(light, 0, 1))))
      const n: N = hash(px, u.seed).sub(0.5).mul(sigma)
      out.assign(ramp(v.add(n)))
    }).Else(() => {
      // LWIR background: calendar surface temperature, albedo (dark surfaces absorb more), height above the ground
      // (structures keep heat), orientation to the sun by day (normals from the screen-space derivatives of the
      // reconstructed position); each term centred on a typical pixel, so the background sits mid-range (pass.ts AGC)
      const nRaw: N = normalize(cross(dFdx(world) as N, dFdy(world) as N))
      const nrm: N = select(dot(nRaw, world.sub(u.camPos)).greaterThan(0), nRaw.negate(), nRaw)
      const sunDot: N = max(dot(nrm, u.sun), 0)
      const struct: N = smoothstep(1.5, 8, world.y.sub(u.groundY))
      const tK: N = u.tSurf.add(u.albK.mul(float(0.15).sub(Y))).add(u.sunK.mul(sunDot.sub(0.35)).mul(u.day)).add(u.structK.mul(struct.sub(0.5)))
      const tS: N = u.epsBg.mul(tK).add(float(1).sub(u.epsBg).mul(u.tSky))
      const tPath: N = u.tAir.add(exp(u.sigma.negate().mul(dist)).mul(tS.sub(u.tAir)))
      const T: N = select(sky, u.tSky, tPath).toVar()
      // emitters, far to near (pass.ts sorts them): rounded silhouette of the projected box, occluded by nearer surfaces
      const halfRes: N = u.res.mul(0.5)
      const range: N = select(sky, float(1e9), dist)
      Loop(SV.maxEmitters, ({ i }: { i: N }) => {
        If(float(i).greaterThanEqual(u.count), () => {
          Break()
        })
        const a: N = em.load(ivec2(int(0), i))
        const b: N = em.load(ivec2(int(1), i))
        const p: N = ndc.sub(a.xy).mul(halfRes)
        const half: N = max(a.zw.mul(halfRes), vec2(SV.emitterMinHalfPx, SV.emitterMinHalfPx))
        const cov: N = clamp(float(0.5).sub(sdRound(p, half, min(half.x, half.y).mul(0.45))), 0, 1)
        const halfC: N = max(half.mul(b.w), vec2(0.6, 0.6))
        const covC: N = clamp(float(0.5).sub(sdRound(p, halfC, min(halfC.x, halfC.y).mul(0.45))), 0, 1)
        const seen: N = select(range.greaterThanEqual(b.x), float(1), float(0))
        T.assign(mix(T, mix(b.z, b.y, covC), cov.mul(seen)))
      })
      const t0: N = clamp(T.sub(u.lo).div(max(u.hi.sub(u.lo), 1e-3)), 0, 1)
      // fixed-pattern and temporal noise of an uncooled core, a fraction of a grey level
      const n: N = hash(px, u.seed).sub(0.5).mul(0.012).add(hash(px, float(0)).sub(0.5).mul(0.008))
      out.assign(ramp(mix(t0, float(1).sub(t0), u.blackHot).add(n)))
    })
    return vec4(out, 1)
  })() as N
  m.depthNode = depthOut.r
  m.vertexNode = vec4((positionGeometry as N).xy, 0, 1) as N
  m.depthTest = true
  m.depthWrite = true
  // three r186 flips depth functions under a reversed depth buffer (AlwaysDepth would become NEVER): NeverDepth -> GL_ALWAYS
  m.depthFunc = reversedZ ? NeverDepth : AlwaysDepth
  m.fog = false
  return {
    material: m, u,
    bindTargets(color: Texture, depth: DepthTexture): void {
      colorT.value = color
      depthT.value = depth
      depthOut.value = depth
    },
    dispose(): void {
      m.dispose()
      colorPh.dispose()
      depthPh.dispose()
    },
  }
}

/** the emitter table of the LWIR material (SV.maxEmitters rows x EMIT_COLS RGBA32F texels, nearest filtering) */
export function makeEmitterTexture(): DataTexture {
  const t = new DataTexture(new Float32Array(4 * EMIT_COLS * SV.maxEmitters), EMIT_COLS, SV.maxEmitters, RGBAFormat, FloatType)
  t.minFilter = NearestFilter
  t.magFilter = NearestFilter
  t.generateMipmaps = false
  t.needsUpdate = true
  t.name = 'sensorView.emitters'
  return t
}
