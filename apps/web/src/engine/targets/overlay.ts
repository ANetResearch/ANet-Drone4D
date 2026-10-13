// Target and sensitivity-volume overlay (M18-FR-024, NFR-005; M18-M19 PRD §8.2; AWR-04 §10.5). Owner: M18.
// Real-size targets: a box wireframe per target (l x w x h along the heading) with a heading tick, <= 32 targets; the
// sensitivity volumes: translucent faces + outline, <= 64 bodies (sector 48 segments, sphere and hemisphere 24 x 12).
// Three draws at most: box lines and body outlines (one ThinLineBatch each, 1 px native lines, the overlay palette of
// engine/mission/lineBatch) and the body faces (one non-indexed Mesh; two attributes: position and bodyInfo = palette
// index, alpha; three vec3 colour uniforms). No textures, no instancing, no per-frame allocation once the buffers fit:
// within the WebGL2 minimum limits (ADR-086/087: 16 vertex attributes, 256 vertex / 224 fragment uniform vectors).
// Colours are tokens only (SCENE.glyphPalette: 0 g50, 1 r500, 2 g500, 3 g400, 4 g300, 5 g200); red (r500) only for the
// target that owns the viewport red (RedArbiter, unacknowledged target.discovered), everything else neutral.
// Rebuilds are event driven (new rows <= 10 Hz, detail or option changes); the local mesh of a body is built once per
// configuration and only translated when the target moves. Degrade levels (PerfGovernor, §8.2): 0 full, 1 faces off
// (outlines only), 2 bodies of the selected target only, 3 bodies off.
// D2-FX-R2-web round 2 (D2-AC-30): the local meshes are keyed by the body's shape alone (shape, radius, z range, half
// angle, rounded to 1 cm / 1e-4 rad) in a canonical orientation and shared by all targets with that body; the heading is
// applied while copying (sectors and boxes rotate, balls and cylinders do not). The previous key held the target id and a
// 1 degree heading bucket, so every turning walker rebuilt its sector mesh at 10 Hz (allocating), which made this the
// largest main-thread task of the sandbox scene (10-16 ms per frame with 30 walkers). A rebuild that only moved targets
// re-uploads positions only (line info and face info are compared while written).
import { BufferAttribute, BufferGeometry, DoubleSide, Group, Mesh, Sphere, Vector3 } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { attribute, select, uniform, varying, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { ThinLineBatch } from '../mission/lineBatch'
import { outputTransform } from '../shading'
import { bodyFaces, bodyMesh, boxEdges, newMeshBuffers, type IndexedFaces, type MeshBuffers } from './bodyMesh'
import { TARGET_CAP } from './decode'
import type { BodyView, TargetsModel } from './model'
import type { Volume } from './geometry'

type N = any // TSL nodes

/** faceVerts: unique face vertices (sphere 25 x 13 per body), faceIdx: face indices (sphere 24 x 12 x 6 per body) */
export const TARGETS_CAPS = { targets: TARGET_CAP, bodies: 64, boxSegs: TARGET_CAP * 13, bodySegs: 64 * 104, faceVerts: 64 * 325, faceIdx: 64 * 1728 } as const
export const PAL = { G50: 0, R500: 1, G500: 2, G400: 3, G300: 4, G200: 5 } as const
export const FACE_ALPHA = { idle: 0.05, occupied: 0.12, selected: 0.08 } as const

interface LocalMesh extends MeshBuffers {
  /** the copy applies the target heading (sectors and boxes); balls and cylinders are axially symmetric */
  rotates: boolean
  used: number
  /** the faces as an indexed mesh (bodies; null for boxes) */
  faces: IndexedFaces | null
}

export interface OverlayOptions {
  view: BodyView
  modelId: string | null
  selectedId: string | null
  redId: string | null
  showBodies: boolean
}

export class TargetsOverlay {
  readonly root = new Group()
  readonly boxes: ThinLineBatch
  readonly outlines: ThinLineBatch
  readonly faces: Mesh
  private readonly pos: BufferAttribute
  private readonly info: BufferAttribute
  private readonly index: BufferAttribute
  /** face vertices and indices written by the last rebuild, and those uploaded */
  private nFace = 0
  private nIdx = 0
  private committedFace = -1
  private committedIdx = -1
  private faceInfoDirty = true
  private faceIdxDirty = true
  /** canonical local meshes by shape key (see the header); used marks the keys of the last rebuild */
  private readonly local = new Map<string, LocalMesh>()
  /** canonical boxes (yaw 0) by size key */
  private readonly boxMesh = new Map<string, LocalMesh>()
  private gen = 0
  private seenVersion = -1
  private seenDetail = -1
  private seenKey = ''
  /** PerfGovernor level (see the header) */
  level = 0
  nBodies = 0
  nTargets = 0

  constructor() {
    this.boxes = new ThinLineBatch(TARGETS_CAPS.boxSegs, 'TargetBoxes', 22)
    this.outlines = new ThinLineBatch(TARGETS_CAPS.bodySegs, 'TargetBodyOutlines', 21)
    const g = new BufferGeometry()
    this.pos = new BufferAttribute(new Float32Array(TARGETS_CAPS.faceVerts * 3), 3)
    this.info = new BufferAttribute(new Float32Array(TARGETS_CAPS.faceVerts * 2), 2)
    this.index = new BufferAttribute(new Uint32Array(TARGETS_CAPS.faceIdx), 1)
    this.pos.setUsage(35048)
    this.info.setUsage(35048)
    this.index.setUsage(35048)
    g.setAttribute('position', this.pos)
    g.setAttribute('bodyInfo', this.info)
    g.setIndex(this.index)
    g.setDrawRange(0, 0)
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    const m = new MeshBasicNodeMaterial()
    const p = SCENE.glyphPalette
    const cHi: N = uniform(new Vector3(p[PAL.G500][0], p[PAL.G500][1], p[PAL.G500][2]))
    const cMid: N = uniform(new Vector3(p[PAL.G400][0], p[PAL.G400][1], p[PAL.G400][2]))
    const cLo: N = uniform(new Vector3(p[PAL.G300][0], p[PAL.G300][1], p[PAL.G300][2]))
    const bi: N = attribute('bodyInfo', 'vec2')
    const rgb: N = select(bi.x.lessThan(PAL.G400 - 0.5), cHi, select(bi.x.lessThan(PAL.G300 - 0.5), cMid, cLo))
    m.colorNode = vec4(varying(outputTransform(rgb), 'vTgtBody') as N, 1) as N
    m.userData.awrOutputInVertex = true
    m.opacityNode = varying(bi.y, 'vTgtAlpha') as N
    m.transparent = true
    m.depthWrite = false
    m.side = DoubleSide
    m.forceSinglePass = true
    m.fog = false
    this.faces = new Mesh(g, m)
    this.faces.frustumCulled = false
    this.faces.renderOrder = 20
    this.faces.name = 'TargetBodyFaces'
    this.faces.visible = false
    this.root.add(this.faces, this.outlines.obj, this.boxes.obj)
    this.root.name = 'TargetsLayer'
  }

  drawCount(): number {
    if (!this.root.visible) return 0
    return this.boxes.drawCount() + this.outlines.drawCount() + (this.faces.visible ? 1 : 0)
  }

  /** rebuild when the model or the options changed; returns true when it rebuilt */
  update(model: TargetsModel, o: OverlayOptions): boolean {
    const key = `${o.view}|${o.modelId}|${o.selectedId}|${o.redId}|${o.showBodies}|${this.level}`
    if (model.version === this.seenVersion && model.detailVersion === this.seenDetail && key === this.seenKey) return false
    this.seenVersion = model.version
    this.seenDetail = model.detailVersion
    this.seenKey = key
    const gen = ++this.gen
    const R = model.rows
    this.boxes.begin()
    this.outlines.begin()
    this.nFace = 0
    this.nIdx = 0
    let nb = 0
    const P = this.pos.array as Float32Array
    const I = this.info.array as Float32Array
    const X = this.index.array as Uint32Array
    const nT = Math.min(R.n, TARGETS_CAPS.targets)
    for (let i = 0; i < nT; i++) {
      const id = model.idOfRow(i)
      const cls = model.classOfRow(i)
      const x = R.pos[3 * i], y = R.pos[3 * i + 1], z = R.pos[3 * i + 2]
      const hc = Math.cos(R.heading[i]), hs = Math.sin(R.heading[i])
      const [l, w, h] = model.sizeOf(id, cls)
      const pal = id === o.redId ? PAL.R500 : id === o.selectedId ? PAL.G50 : R.enabled[i] ? PAL.G400 : PAL.G200
      const box = this.boxOf(l, w, h, gen)
      this.boxes.segsXform(box.seg, box.nSeg, x, y, z, hc, hs, pal, R.enabled[i] ? 1 : 0.5)
      if (!o.showBodies || this.level >= 3 || !R.enabled[i] || (this.level >= 2 && id !== o.selectedId)) continue
      const vols = model.volumesOfRow(i, o.view, o.modelId)
      const occupied = R.occupied[i] === 1
      for (const v of vols) {
        if (nb >= TARGETS_CAPS.bodies) break
        nb++
        const mesh = this.localMesh(v, gen)
        const c = mesh.rotates ? Math.cos(v.yawRad) : 1
        const sn = mesh.rotates ? Math.sin(v.yawRad) : 0
        const cx = v.center[0], cy = v.center[1], cz = v.center[2]
        const opal = id === o.selectedId || occupied ? PAL.G500 : v.kind === 'visual' ? PAL.G300 : PAL.G200
        this.outlines.segsXform(mesh.seg, mesh.nSeg, cx, cy, cz, c, sn, opal, 0.8)
        if (this.level >= 1) continue
        const fpal = occupied ? PAL.G500 : v.kind === 'visual' ? PAL.G400 : PAL.G300
        // compared with the stored float32 value (FACE_ALPHA are not float32 numbers)
        const alpha = Math.fround(occupied ? FACE_ALPHA.occupied : id === o.selectedId ? FACE_ALPHA.selected : FACE_ALPHA.idle)
        const f = mesh.faces
        if (!f) continue
        const need = this.nFace + f.nVert
        const needI = this.nIdx + f.nIdx
        if (need > TARGETS_CAPS.faceVerts || needI > TARGETS_CAPS.faceIdx) continue
        const t = f.pos
        let a = 3 * this.nFace
        let b = 2 * this.nFace
        for (let k = 0; k < f.nVert; k++) {
          const q = 3 * k
          const lx = t[q], ly = t[q + 1]
          P[a] = cx + c * lx - sn * ly
          P[a + 1] = cy + sn * lx + c * ly
          P[a + 2] = cz + t[q + 2]
          if (I[b] !== fpal || I[b + 1] !== alpha) {
            I[b] = fpal
            I[b + 1] = alpha
            this.faceInfoDirty = true
          }
          a += 3
          b += 2
        }
        const base = this.nFace
        let e = this.nIdx
        for (let k = 0; k < f.nIdx; k++) {
          const vi = base + f.idx[k]
          if (X[e] !== vi) {
            X[e] = vi
            this.faceIdxDirty = true
          }
          e++
        }
        this.nFace = need
        this.nIdx = needI
      }
    }
    // the canonical meshes not used by this rebuild (bodies or sizes gone) are released
    for (const [k, m] of this.local) if (m.used !== gen) this.local.delete(k)
    for (const [k, m] of this.boxMesh) if (m.used !== gen) this.boxMesh.delete(k)
    this.nBodies = nb
    this.nTargets = nT
    this.boxes.commitMoved()
    this.outlines.commitMoved()
    this.faces.geometry.setDrawRange(0, this.nIdx)
    this.faces.visible = this.nIdx > 0
    if (this.nIdx > 0) {
      this.pos.clearUpdateRanges()
      this.pos.addUpdateRange(0, this.nFace * 3)
      this.pos.needsUpdate = true
      if (this.faceInfoDirty || this.nFace !== this.committedFace) {
        this.info.clearUpdateRanges()
        this.info.addUpdateRange(0, this.nFace * 2)
        this.info.needsUpdate = true
      }
      if (this.faceIdxDirty || this.nIdx !== this.committedIdx) {
        this.index.clearUpdateRanges()
        this.index.addUpdateRange(0, this.nIdx)
        this.index.needsUpdate = true
      }
      this.faceInfoDirty = false
      this.faceIdxDirty = false
      this.committedFace = this.nFace
      this.committedIdx = this.nIdx
    }
    return true
  }

  /** canonical box wireframe (yaw 0) of a target size */
  private boxOf(l: number, w: number, h: number, gen: number): LocalMesh {
    const key = `${Math.round(l * 100)}|${Math.round(w * 100)}|${Math.round(h * 100)}`
    let m = this.boxMesh.get(key)
    if (!m) {
      m = { ...newMeshBuffers(0, 13), rotates: true, used: gen, faces: null }
      boxEdges(l, w, h, 0, m)
      this.boxMesh.set(key, m)
    }
    m.used = gen
    return m
  }

  /**
   * canonical local mesh of a world volume: its shape relative to the foot point, sectors centred on +x (the copy rotates
   * them by the volume yaw, which already holds the azimuth offset); shared by every body with the same shape
   */
  private localMesh(v: Volume, gen: number): LocalMesh {
    const sector = v.shape === 'sector'
    const ball = v.shape === 'sphere' || v.shape === 'hemisphere'
    const zLo = ball ? 0 : Math.round((v.zMinM - v.center[2]) * 100)
    const zHi = ball ? 0 : Math.round((v.zMaxM - v.center[2]) * 100)
    const key = `${v.shape}|${Math.round(v.rM * 100)}|${zLo}|${zHi}|${sector ? Math.round(v.halfAngleRad * 1e4) : 0}`
    let m = this.local.get(key)
    if (!m) {
      const body = { kind: v.kind, shape: v.shape, radius_max_m: v.rM, z_min_m: zLo / 100, z_max_m: zHi / 100, half_angle_deg: (v.halfAngleRad * 180) / Math.PI,
        azimuth_offset_deg: 0 }
      m = { ...newMeshBuffers(0, 128), rotates: sector, used: gen, faces: bodyFaces(body, 0, v.shape !== 'sector', v.rM) }
      bodyMesh(body, 0, v.shape !== 'sector', m, v.rM)
      m.tri = new Float32Array(0) // the faces are drawn from the indexed copy
      m.nTri = 0
      this.local.set(key, m)
    }
    m.used = gen
    return m
  }

  setVisible(v: boolean): void {
    this.root.visible = v
  }

  dispose(): void {
    this.boxes.dispose()
    this.outlines.dispose()
    this.faces.geometry.dispose()
    ;(this.faces.material as MeshBasicNodeMaterial).dispose()
    this.local.clear()
  }
}
