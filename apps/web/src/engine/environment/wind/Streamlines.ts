// AWSL streamlines of the analytic field (D1-ext; M07-FR-048; M07 §6.8.3; g06 §7.4; n04 §3.3.1; 15 §10.8). Owner: M07.
// The server generates (field_id, whole degree) sets (vis.streamlines + /d{deg:03d}.awsl, immutable). Each segment is a
// flat quad 1 raster pixel wide (6 vertices, attribute-less; segment data in an RGBA32F texture, 3 texels per segment:
// (p0, tau0), (p1, tau1), (s0, s1, 0, 0)); dashes move with phi = fract((tau_hat - S) / 48 m), S the anchor integral of
// speed_ref (reduced mod 48 on the CPU), so a wind speed change keeps the geometry and the phase continuous; alpha =
// 0.6 smoothstep(0, 0.04, phi) (1 - phi)^3 T(camera -> p); colour by s f(z) in the 5 fixed bins of --wind-ramp-1..5. A
// direction change of >= 1 degree loads another set at most twice per second and cross-fades over --duration-slow. Density
// is a prefix of the shuffled lines (drawRange). Not available on Tier S (n04: 500 lines x 32 segments cost +32 ms).
import { CustomBlending, DataTexture, FloatType, NearestFilter, OneFactor, OneMinusSrcAlphaFactor, RGBAFormat, Vector3, type Mesh } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import {
  Fn, cameraProjectionMatrix, clamp, cross, exp, float, floor, fract, int, ivec2, length, max, mix, modelViewMatrix, normalize, pow, select,
  smoothstep, textureLoad, uniform, varyingProperty, vec3, vec4,
} from 'three/tsl'
import { MOTION } from '@/lib/tokens/motion.gen'
import { SCENE } from '@/lib/tokens/scene.gen'
import { opticalDepthNode } from '../atmosphere/fogNode'
import { makeEnvNodes, type EnvNodes, type EnvParams } from '../lighting/EnvUniforms'
import { ENV_TIERS } from '../quality/envTiers'
import { quadCorner, quadMesh } from '../precip/flatQuads'

type N = any // TSL nodes

export const AWSL_MAGIC = 0x4c535741
const TEX_W = 1024
export const DASH_M = 48

export interface AwslSet { dirFromDeg: number; nLines: number; nVerts: number; offsets: Uint32Array; verts: Float32Array; segStart: Uint32Array }

export function decodeAWSL(buf: ArrayBuffer): AwslSet {
  const dv = new DataView(buf)
  if (dv.getUint32(0, true) !== AWSL_MAGIC || dv.getUint16(4, true) !== 1 || dv.getUint32(28, true) !== 20) throw new Error('AWSL header invalid')
  const nLines = dv.getUint32(8, true)
  const nVerts = dv.getUint32(12, true)
  const ob = 4 * (nLines + 1)
  const offsets = new Uint32Array(buf.slice(48, 48 + ob))
  const start = 48 + ob + ((8 - (ob % 8)) % 8)
  const verts = new Float32Array(buf.slice(start, start + nVerts * 20))
  if (offsets[nLines] !== nVerts) throw new Error('AWSL offsets invalid')
  const segStart = new Uint32Array(nLines + 1)
  for (let i = 0; i < nLines; i++) segStart[i + 1] = segStart[i] + Math.max(0, offsets[i + 1] - offsets[i] - 1)
  return { dirFromDeg: dv.getFloat32(16, true), nLines, nVerts, offsets, verts, segStart }
}

class StreamSet {
  readonly mesh: Mesh
  readonly nodes: EnvNodes
  readonly tex: DataTexture
  readonly fade: N
  readonly phaseU: N
  segs = 0

  constructor(p: EnvParams, maxSegs: number) {
    this.nodes = makeEnvNodes(p)
    const n = this.nodes
    const h = Math.max(1, Math.ceil((3 * maxSegs) / TEX_W))
    this.tex = new DataTexture(new Float32Array(TEX_W * h * 4), TEX_W, h, RGBAFormat, FloatType)
    this.tex.minFilter = NearestFilter
    this.tex.magFilter = NearestFilter
    this.tex.needsUpdate = true
    this.fade = uniform(0)
    const tex = this.tex
    const fade = this.fade
    const vTau = varyingProperty('float', 'vSlTau')
    const vA = varyingProperty('float', 'vSlA')
    const vCol = varyingProperty('vec3', 'vSlCol')
    const ramp = SCENE.windRamp.map((c) => uniform(new Vector3(c[0], c[1], c[2])))
    const texel = (i: N): N => textureLoad(tex, ivec2(i.bitAnd(TEX_W - 1), i.shiftRight(10)))
    const m = new MeshBasicNodeMaterial()
    m.vertexNode = Fn(() => {
      const { iid, corner } = quadCorner()
      const b: N = int(iid).mul(3)
      const a0: N = texel(b)
      const a1: N = texel(b.add(1))
      const s: N = texel(b.add(2))
      const y01: N = corner.y.mul(0.5).add(0.5)
      const p: N = mix(a0.xyz, a1.xyz, y01).toVar()
      const dir: N = normalize(a1.xyz.sub(a0.xyz))
      const toCam: N = n.camEnu.sub(p)
      const dist: N = max(length(toCam), float(1e-3))
      const cw: N = cross(dir, toCam.div(dist))
      const side: N = select(length(cw).greaterThan(1e-4), normalize(cw), vec3(0, 0, 1))
      const pos: N = p.add(side.mul(corner.x.mul(0.5).mul(dist).mul(n.pixelWorldScale).mul(n.dpr.max(1))))
      vTau.assign(mix(a0.w, a1.w, y01))
      const spd: N = n.windS.mul(mix(s.x, s.y, y01))
      const bin: N = floor(clamp(spd.div(4), float(0), float(4)))
      vCol.assign(select(bin.lessThan(0.5), ramp[0], select(bin.lessThan(1.5), ramp[1], select(bin.lessThan(2.5), ramp[2], select(bin.lessThan(3.5), ramp[3], ramp[4])))))
      const T: N = exp(opticalDepthNode(n, n.camEnu.z.sub(n.groundZ), toCam.z.negate().div(dist), dist).negate())
      vA.assign(T.mul(fade).mul(n.visualOn))
      return cameraProjectionMatrix.mul(modelViewMatrix).mul(vec4(pos, 1))
    })() as N
    this.phaseU = uniform(0)
    const phase: N = this.phaseU
    m.colorNode = Fn(() => {
      const phi: N = fract(vTau.sub(phase).div(DASH_M))
      const a: N = vA.mul(0.6).mul(smoothstep(float(0), float(0.04), phi)).mul(pow(float(1).sub(phi), float(3)))
      return vec4(vCol.mul(a), a)
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
    this.mesh = quadMesh(m, 'EnvStreamlines', ENV_TIERS.order.streamlines)
  }

  load(set: AwslSet, maxLines: number): void {
    const d = this.tex.image.data as Float32Array
    const nl = Math.min(set.nLines, maxLines)
    const cap = Math.floor(d.length / 12)
    let k = 0
    for (let l = 0; l < nl; l++) {
      for (let v = set.offsets[l]; v + 1 < set.offsets[l + 1] && k < cap; v++, k++) {
        const a = 5 * v
        const o = 12 * k
        d[o] = set.verts[a]
        d[o + 1] = set.verts[a + 1]
        d[o + 2] = set.verts[a + 2]
        d[o + 3] = set.verts[a + 3]
        d[o + 4] = set.verts[a + 5]
        d[o + 5] = set.verts[a + 6]
        d[o + 6] = set.verts[a + 7]
        d[o + 7] = set.verts[a + 8]
        d[o + 8] = set.verts[a + 4]
        d[o + 9] = set.verts[a + 9]
        d[o + 10] = 0
        d[o + 11] = 0
      }
    }
    this.segs = k
    this.tex.needsUpdate = true
    this.mesh.geometry.setDrawRange(0, 6 * k)
  }
}

export class Streamlines {
  private readonly sets: [StreamSet, StreamSet]
  private cur = 0
  private t0 = Number.NEGATIVE_INFINITY
  private lastLoadMs = Number.NEGATIVE_INFINITY
  private wantDeg = -1
  private loadedDeg = -1
  private loading = false
  /** a new set arrived: the cross-fade starts at the next update (frame clock) */
  private arrived = false
  enabled = false

  constructor(p: EnvParams, private readonly fetchSet: (url: string) => Promise<ArrayBuffer>, readonly maxLines = 1000) {
    this.sets = [new StreamSet(p, maxLines * 63), new StreamSet(p, maxLines * 63)]
  }

  get meshes(): Mesh[] {
    return [this.sets[0].mesh, this.sets[1].mesh]
  }

  /** per frame: direction (deg, from), URL prefix (vis.streamlines), anchor S (m), wall clock */
  update(dirFromDeg: number, prefix: string | null, sM: number, nowMs: number, reduced: boolean): void {
    const on = this.enabled && !!prefix
    const deg = ((Math.round(dirFromDeg) % 360) + 360) % 360
    if (on && deg !== this.loadedDeg && !this.loading && nowMs - this.lastLoadMs >= 500) {
      this.wantDeg = deg
      this.loading = true
      this.lastLoadMs = nowMs
      const url = `${prefix}/d${String(deg).padStart(3, '0')}.awsl`
      this.fetchSet(url).then((b) => {
        const nxt = 1 - this.cur
        this.sets[nxt].load(decodeAWSL(b), this.maxLines)
        this.cur = nxt
        this.loadedDeg = this.wantDeg
        this.arrived = true
      }).catch(() => {
        this.loadedDeg = this.wantDeg // do not retry the same set in a loop
      }).finally(() => {
        this.loading = false
      })
    }
    if (this.arrived) {
      this.arrived = false
      this.t0 = nowMs
    }
    const x = reduced ? 1 : Math.min(Math.max((nowMs - this.t0) / MOTION.durationSlowMs, 0), 1)
    const ph = ((sM % DASH_M) + DASH_M) % DASH_M
    for (let i = 0; i < 2; i++) {
      const s = this.sets[i]
      const f = !on ? 0 : i === this.cur ? x : 1 - x
      s.fade.value = f
      s.phaseU.value = ph
      s.mesh.visible = on && f > 0 && s.segs > 0
    }
  }

  drawCount(): number {
    return (this.sets[0].mesh.visible ? 1 : 0) + (this.sets[1].mesh.visible ? 1 : 0)
  }

  dispose(): void {
    for (const s of this.sets) {
      s.mesh.geometry.dispose()
      ;(s.mesh.material as MeshBasicNodeMaterial).dispose()
      s.tex.dispose()
    }
  }
}
