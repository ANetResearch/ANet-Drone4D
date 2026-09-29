// Drone markers (M06-FR-033; AWR-15 §10.4; g01 §3). Owner: M06.
// One attribute-less flat-quad draw (drawRange = 6 n) for every vehicle in the marker bucket: diameter 6 CSS px filled
// with --drone-marker plus a 1 px --drone-halo ring (SDF circle), --drone-stale when HOLD or stale, r500 when the
// vehicle is the red entity. Position and style code come from DroneStateTex (RGBA32F, width 1024, texel = pos.xyz +
// style) updated every frame (n x 16 B). Vehicles drawn as models (low-poly, hero) or hidden (FPV focus) use style 3
// and collapse outside the view volume. Size in raster px = (6 + 2) x ctx.dpr (M06 §6.5).
import { BufferGeometry, Mesh, Sphere, Vector2, Vector3, type DataTexture } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, length, select, uniform, varyingProperty, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { clipQuad, hiddenClip, instanceTexel, makeInstanceTexture, quadVertex } from './screenQuad'

type N = any // TSL nodes

export const MARKER = { cssPx: 6, haloPx: 1, texLog2W: 10 } as const
export const MarkerStyle = { Normal: 0, Stale: 1, Red: 2, Hidden: 3 } as const
export type MarkerStyleCode = (typeof MarkerStyle)[keyof typeof MarkerStyle]

export class MarkerBatch {
  readonly mesh: Mesh
  readonly tex: DataTexture
  readonly data: Float32Array
  private readonly uSize: N
  private readonly uViewport: N
  n = 0

  constructor(readonly capacity: number) {
    const w = 1 << MARKER.texLog2W
    this.tex = makeInstanceTexture(w, Math.max(1, Math.ceil(capacity / w)), 'DroneStateTex')
    this.data = this.tex.image.data as Float32Array
    this.uSize = uniform(8)
    this.uViewport = uniform(new Vector2(1, 1))
    const vQ = varyingProperty('vec2', 'vMarkerQ')
    const vStyle = varyingProperty('float', 'vMarkerStyle')
    const m = new MeshBasicNodeMaterial()
    const tex = this.tex
    const size = this.uSize
    const vp = this.uViewport
    m.vertexNode = Fn(() => {
      const { iid, corner } = quadVertex()
      const t0: N = instanceTexel(tex, iid, 0, 1, MARKER.texLog2W)
      vQ.assign(corner)
      vStyle.assign(t0.w)
      return select(t0.w.greaterThan(2.5), hiddenClip(), clipQuad(t0.xyz, corner, size.mul(0.5), vp))
    })() as N
    const fill: N = uniform(new Vector3(...SCENE.droneMarker))
    const stale: N = uniform(new Vector3(...SCENE.droneStale))
    const red: N = uniform(new Vector3(...SCENE.glyphPalette[1]))
    const halo: N = uniform(new Vector3(...SCENE.droneHalo))
    const inner = MARKER.cssPx / (MARKER.cssPx + 2 * MARKER.haloPx)
    m.colorNode = Fn(() => {
      const r: N = length(vQ)
      r.greaterThan(1).discard()
      const base: N = select(vStyle.greaterThan(1.5), red, select(vStyle.greaterThan(0.5), stale, fill))
      return vec4(select(r.greaterThan(inner), halo, base) as N, 1)
    })() as N
    m.depthTest = false
    m.depthWrite = false
    m.transparent = true
    m.fog = false
    const g = new BufferGeometry()
    g.setDrawRange(0, 0)
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    this.mesh = new Mesh(g, m)
    this.mesh.frustumCulled = false
    this.mesh.renderOrder = 89
    this.mesh.name = 'DroneMarkers'
    this.mesh.visible = false
  }

  /** write instance i (ENU m, style code) */
  set(i: number, x: number, y: number, z: number, style: number): void {
    const d = this.data
    const o = 4 * i
    d[o] = x
    d[o + 1] = y
    d[o + 2] = z
    d[o + 3] = style
  }

  /** after writing n instances: upload, draw range, raster size (CSS px x dpr) */
  commit(n: number, dpr: number, dbW: number, dbH: number): void {
    this.n = n
    this.mesh.geometry.setDrawRange(0, 6 * n)
    this.mesh.visible = n > 0
    this.uSize.value = Math.max(2, (MARKER.cssPx + 2 * MARKER.haloPx) * dpr)
    ;(this.uViewport.value as Vector2).set(Math.max(1, dbW), Math.max(1, dbH))
    if (n > 0) {
      this.tex.needsUpdate = true
    }
  }

  drawCount(): number {
    return this.mesh.visible ? 1 : 0
  }

  dispose(): void {
    this.mesh.geometry.dispose()
    ;(this.mesh.material as MeshBasicNodeMaterial).dispose()
    this.tex.dispose()
  }
}

