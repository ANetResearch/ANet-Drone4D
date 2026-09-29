// Low wind arrows (M07-FR-042; M07 §6.8.2 item 7; 15 §10.8; n04 §3.3.4). Owner: M07.
// 24 x 24 arrows, one flat quad each (6 vertices, id = vertexIndex / 6, same attribute-less framework as the rain), on an
// AGL slice (10 / 50 / 120 m, default 50) above the DTM, origin = orbit focus snapped to the spacing (octaves 10-160 m
// by camera distance). Each arrow evaluates W_vis = windAtEnu (Low: mean + vertical + fronts; Med adds turbulence) at
// its base, is stretched along W_vis (pitch from w) with the width facing the camera, length min(|W| / vmax, 1) 0.9
// spacing (vmax = vis.vmax_mps, 20 m/s in D1); the fragment draws a 1.5 px shaft, a 6 px head and a 1 px --drone-halo by
// SDF; colour by |W| in 5 fixed bins (0-4, 4-8, 8-12, 12-16, >= 16 m/s) from --wind-ramp-1..5 (no continuous blend),
// opacity 0.8 times the transmittance. Tier S counts 1 arrow as 1 quad equivalent of the 2000 shared with precipitation.
import { CustomBlending, OneFactor, OneMinusSrcAlphaFactor, Vector3, type Mesh } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import {
  Fn, abs, cameraProjectionMatrix, clamp, cross, exp, float, floor, length, max, min, modelViewMatrix, normalize, select, uniform,
  varyingProperty, vec3, vec4,
} from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { opticalDepthNode } from '../atmosphere/fogNode'
import { makeEnvNodes, type EnvNodes, type EnvParams } from '../lighting/EnvUniforms'
import { ENV_TIERS, arrowCount } from '../quality/envTiers'
import { hiddenClip, quadCorner, quadMesh } from '../precip/flatQuads'
import { dtmHeight, makeDtmNodes, type DtmNodes, type EnvTerrain } from '../terrain/dtmSampler'
import { windAtEnu } from './windNode'

type N = any // TSL nodes

export class WindArrows {
  readonly mesh: Mesh
  readonly nodes: EnvNodes
  readonly dtm: DtmNodes
  readonly count = arrowCount()

  constructor(p: EnvParams, terrain: EnvTerrain, opts: { turb: boolean; turbTex?: N } = { turb: false }) {
    this.nodes = makeEnvNodes(p)
    this.dtm = makeDtmNodes(terrain)
    const n = this.nodes
    const dtm = this.dtm
    const G = ENV_TIERS.arrowGrid
    const vAlong = varyingProperty('float', 'vArrowAlong')
    const vAcross = varyingProperty('float', 'vArrowAcross')
    const vLenPx = varyingProperty('float', 'vArrowLenPx')
    const vHalfPx = varyingProperty('float', 'vArrowHalfPx')
    const vCol = varyingProperty('vec3', 'vArrowCol')
    const vA = varyingProperty('float', 'vArrowA')
    const ramp = SCENE.windRamp.map((c) => uniform(new Vector3(c[0], c[1], c[2])))
    const halo: N = uniform(new Vector3(...SCENE.droneHalo))
    const m = new MeshBasicNodeMaterial()
    m.vertexNode = Fn(() => {
      const { iid, corner } = quadCorner()
      const gi: N = iid.sub(floor(iid.div(G)).mul(G))
      const gj: N = floor(iid.div(G))
      const bx: N = n.arrowOrigin.x.add(gi.sub((G - 1) / 2).mul(n.arrowSpacing))
      const by: N = n.arrowOrigin.y.add(gj.sub((G - 1) / 2).mul(n.arrowSpacing))
      const bz: N = dtmHeight(dtm, vec3(bx, by, 0).xy, dtm.tex).add(n.arrowSliceAgl)
      const base: N = vec3(bx, by, bz).toVar()
      const w: N = windAtEnu(n, base, { turb: opts.turb, turbTex: opts.turbTex, dtm, dtmTex: dtm.tex }).toVar()
      const spd: N = length(w)
      const len: N = min(spd.div(max(n.vmax, float(1e-3))), float(1)).mul(ENV_TIERS.arrowLenFrac).mul(n.arrowSpacing)
      const a: N = select(spd.greaterThan(1e-4), w.div(max(spd, float(1e-6))), vec3(1, 0, 0))
      const toCam: N = n.camEnu.sub(base)
      const dist: N = max(length(toCam), float(1e-3))
      const cw: N = cross(a, toCam.div(dist))
      const side: N = select(length(cw).greaterThan(1e-4), normalize(cw), vec3(0, 0, 1))
      const pxW: N = dist.mul(n.pixelWorldScale) // metres per raster pixel at the arrow
      const halfPx: N = float(ENV_TIERS.arrowHeadPx / 2 + ENV_TIERS.arrowHaloPx + 1).mul(n.dpr.max(1))
      const lenPx: N = len.div(max(pxW, float(1e-6)))
      const y01: N = corner.y.mul(0.5).add(0.5)
      const pos: N = base.add(a.mul(y01.mul(len))).add(side.mul(corner.x.mul(halfPx).mul(pxW)))
      vAlong.assign(y01.mul(lenPx))
      vAcross.assign(corner.x.mul(halfPx))
      vLenPx.assign(lenPx)
      vHalfPx.assign(halfPx)
      const bin: N = floor(clamp(spd.div(4), float(0), float(4)))
      vCol.assign(select(bin.lessThan(0.5), ramp[0], select(bin.lessThan(1.5), ramp[1], select(bin.lessThan(2.5), ramp[2], select(bin.lessThan(3.5), ramp[3], ramp[4])))))
      const T: N = exp(opticalDepthNode(n, n.camEnu.z.sub(n.groundZ), toCam.z.negate().div(dist), dist).negate())
      vA.assign(T.mul(ENV_TIERS.arrowOpacity).mul(n.visualOn))
      return select(lenPx.lessThan(3), hiddenClip(), cameraProjectionMatrix.mul(modelViewMatrix).mul(vec4(pos, 1)))
    })() as N
    m.colorNode = Fn(() => {
      const d: N = n.dpr.max(1)
      const shaft: N = float(ENV_TIERS.arrowShaftPx / 2).mul(d)
      const head: N = float(ENV_TIERS.arrowHeadPx / 2).mul(d)
      const headLen: N = min(float(8).mul(d), vLenPx.mul(0.5))
      const u: N = abs(vAcross)
      const t: N = vLenPx.sub(vAlong) // distance to the tip along the arrow
      const inHead: N = t.lessThan(headLen)
      const hw: N = select(inHead, head.mul(t.div(max(headLen, float(1e-3)))), shaft)
      const dist: N = u.sub(hw) // > 0 outside the arrow body across
      const endGap: N = max(vAlong.negate(), t.negate())
      const sd: N = max(dist, endGap)
      const haloPx: N = float(ENV_TIERS.arrowHaloPx).mul(d)
      sd.greaterThan(haloPx).discard()
      const col: N = select(sd.lessThanEqual(0), vCol, halo)
      const a: N = vA
      return vec4(col.mul(a), a)
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
    this.mesh = quadMesh(m, 'EnvArrows', ENV_TIERS.order.arrows)
  }

  setVisible(on: boolean): void {
    this.mesh.geometry.setDrawRange(0, on ? 6 * this.count : 0)
    this.mesh.visible = on
  }

  drawCount(): number {
    return this.mesh.visible ? 1 : 0
  }

  dispose(): void {
    this.mesh.geometry.dispose()
    ;(this.mesh.material as MeshBasicNodeMaterial).dispose()
  }
}
