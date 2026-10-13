// Field view instance data (AWR-04 §15.11; ADR-125; M19-FR-036 consumer). Owner: M06.
// CPU side of the field layer, rebuilt at most once per second (values change at <= 1 Hz, phone 0.5 Hz) or at once on a
// view, mode or selection change; the GPU side moves the anchors every frame (engine/field/FieldRenderer.ts).
// Two kinds of anchor, one point draw:
//   radiation field  one anchor per own vehicle of the shown profile (field/{kind}), pattern index j = its position in the
//                    profile (roster order, as the server's SB-78); point i at r_min (r_max / r_min)^h1 along the Halton
//                    direction, r_max the farthest drawable grid radius; value = profileValue(frame, entry, r, e) with e the
//                    elevation of the vehicle seen from the point (-asin u_z); band 1..5 by the frame's band edges (0 and NaN:
//                    not drawn); grey level and opacity rise with the band (0.15 .. 0.85), the point shape is the modality's.
//                    Points inside the solid shell (the selected target's effective radius toward this vehicle for its model,
//                    else the frame's default person shell) are drawn in the top band, 1 px larger (acoustic and visual).
//   sensitivity      one anchor per (target, enabled body) of field/volumes: pattern index targetOrder * 4 + body index,
//   volume           r_min 1 m, r_max the body's configured radius; only points inside the configured body are kept (the
//                    keep-out prediction's geometry, bodyMask); P = 1 inside a geometric body, or the table model
//                    1 / (1 + exp((r - r_eff(dir)) / (w / 2.2))) for physical bodies and the "physical reference" display;
//                    opacity 0.05 + 0.80 P; grey and shape by modality (acoustic dot g300, visual diamond g50, radar ring
//                    g400, thermal cross g500).
// Shells (one line draw): around each vehicle the solid reference shell and the dashed iso shells of the frame (acoustic:
// background-noise level), each as up to three great circles (equator and two meridians) whose radius follows the
// elevation; the circle count is capped per form factor (fewer circles per shell first, then no dashed shells, then the
// farthest vehicles go).
// Encoding (single draw, no hue, R2): point offset vec3 (u * r, ENU m) and info vec4 (anchor slot, code = ramp + 5 * shape,
// opacity, diameter in CSS px); shell vertex offset vec3 and info vec4 (anchor slot, ramp, dash coordinate or -1, opacity).
// Ramp 0..4 = --field-ramp-1..5 (g500, g400, g300, g200, g50). Free-field display: no terrain occlusion (the pair readout
// carries the line of sight).
import {
  azRel, bodyMask, isoRadius, profileBand, profileValue, tableRef, volumeAnchorIndex, volumeP, volumeREff, N_R, type FieldKind, type ProfileEntry,
  type ProfileFrame, type VolumeBody, type VolumesFrame,
} from '../perception/field'
import type { SensitivityBody } from '../targets/geometry'
import { allocatePoints, FIELD_LEVEL, type FieldBudget } from './budget'
import { PATTERN, patternDir, patternRadius } from './pattern'

export const FIELD = {
  anchors: 64,
  circleSegs: 48,
  dashesPerCircle: 24,
  /** opacity of band 1 and band 5 points; of P = 0 and P = 1 volume points */
  bandAlpha: [0.15, 0.85] as const,
  volAlpha: [0.05, 0.8] as const,
  ramp: { solid: 3, highlight: 4, iso: 2, top: 4 },
  shellAlpha: 0.9,
  isoAlpha: 0.75,
} as const

/** point shapes (fragment distance fields): dot, diamond, ring, cross */
export const FIELD_SHAPE: Readonly<Record<FieldKind, number>> = { acoustic: 0, visual: 1, radar: 2, thermal: 3 }
/** grey of a modality (ramp index): acoustic g300, visual g50, radar g400, thermal g500 */
export const MODAL_RAMP: Readonly<Record<FieldKind, number>> = { acoustic: 2, visual: 4, radar: 1, thermal: 0 }
const DEG = 180 / Math.PI

export type VolumesMode = 'off' | 'selected' | 'all'

export interface UavLookup {
  /** roster agent number of a vehicle id, -1 when unknown */
  agentNo(id: string): number
  /** rendered ENU position */
  pose(agentNo: number, out: Float64Array): boolean
}
export interface TargetLookup {
  /** row of a target in the latest rows, -1 when unknown */
  rowOf(id: string): number
  /** a number identifying the target in a row (its agent number): a stored row is still valid while it matches */
  keyOf(row: number): number
  pos(row: number, out: Float64Array): void
  yaw(row: number): number
  /** configured sensitivity body `idx` of a target */
  body(id: string, row: number, idx: number): SensitivityBody | null
}

export interface FieldBuildInput {
  kind: FieldKind | 'none'
  profile: ProfileFrame | null
  volumes: VolumesFrame | null
  volumesMode: VolumesMode
  physicalRef: boolean
  selectedTarget: string | null
  /** readout-row highlight: the solid shell of this vehicle in the top grey */
  highlightUav: string | null
  /** vehicle model of the volumes: the primary selection's model (null: the first vehicle of the profile, else models[0]) */
  volumeModel: string | null
  budget: FieldBudget
  /** PerfGovernor level of the field knob (FIELD_LEVEL) */
  level: number
  /** ENU eye position (nearest anchors win when over a cap) */
  camera: Float64Array | null
  uavs: UavLookup
  targets: TargetLookup | null
}

export interface FieldAnchor {
  kind: 'uav' | 'body'
  /** vehicle id, or the target id of a body */
  id: string
  agentNo: number
  /** body anchors: row at the build and its key (TargetLookup.keyOf); the per-frame lookup re-resolves only when it moved */
  row: number
  rowKey: number
  bodyIdx: number
  /** pattern index */
  j: number
  points: number
  /** body anchors: yaw at the build (a turn of the target triggers a rebuild of its mask) */
  yaw: number
}

export interface FieldStats {
  kind: FieldKind | 'none'
  uavAnchors: number
  bodyAnchors: number
  points: number
  /** points drawn in the top band because they lie inside the solid shell */
  inShell: number
  circles: number
  segments: number
  /** per kept volume point: min and max P (NaN without volume points) */
  pMin: number
  pMax: number
  modelIdx: number
}

/** preallocated arrays of one build (point and shell-vertex capacities) */
export class FieldBuffers {
  readonly off: Float32Array
  readonly info: Float32Array
  readonly sPos: Float32Array
  readonly sInfo: Float32Array
  n = 0
  nVerts = 0
  readonly anchors: FieldAnchor[] = []
  stats: FieldStats = emptyStats('none')
  /** scratch: world positions and the mask of one body anchor */
  readonly world = new Float64Array(3 * PATTERN.maxPerAnchor)
  readonly radii = new Float64Array(PATTERN.maxPerAnchor)
  readonly dirs = new Float64Array(3 * PATTERN.maxPerAnchor)
  readonly mask = new Uint8Array(PATTERN.maxPerAnchor)

  constructor(readonly capPoints: number, readonly capCircles: number) {
    this.off = new Float32Array(3 * capPoints)
    this.info = new Float32Array(4 * capPoints)
    const verts = 2 * capCircles * FIELD.circleSegs
    this.sPos = new Float32Array(3 * verts)
    this.sInfo = new Float32Array(4 * verts)
  }
  get capVerts(): number {
    return this.sPos.length / 3
  }
}

function emptyStats(kind: FieldKind | 'none'): FieldStats {
  return { kind, uavAnchors: 0, bodyAnchors: 0, points: 0, inShell: 0, circles: 0, segments: 0, pMin: Number.NaN, pMax: Number.NaN, modelIdx: -1 }
}

/**
 * r_max of a vehicle's pattern: where the field falls to the draw floor (the frame's lowest band edge) at the side elevation
 * cell, which is the server's r_max_of for the acoustic, visual and radar profiles (SB-78 samples the same pattern); else
 * (thermal: the floor is the reference camera's NETD, not a band edge) the farthest drawable grid radius; NaN when nothing
 * is drawable
 */
export function anchorRMax(f: ProfileFrame, e: ProfileEntry): number {
  let last = Number.NaN
  for (let k = N_R - 1; k >= 1; k--) {
    if (Number.isFinite(e.vR[k])) {
      last = f.rM[k]
      break
    }
  }
  if (!(last > 0)) return Number.NaN
  const iso = f.bands.length > 0 ? isoRadius(f, e, f.bands[0], 15) : Number.NaN
  return iso > last ? iso : last
}

/** point code: ramp index + 5 * shape */
export const pointCode = (ramp: number, shape: number): number => ramp + 5 * shape

/** body kinds drawn as volumes (rf is schema only, P2) */
const SHOWN_BODY: ReadonlySet<string> = new Set(['visual', 'acoustic', 'radar'])
/** body kinds that name a modality */
const bodyModality = (k: string): FieldKind => (k === 'visual' || k === 'radar' || k === 'thermal' ? k : 'acoustic')

interface BodyCand { id: string; order: number; row: number; body: VolumeBody; cfg: SensitivityBody; dist: number; yaw: number }
interface UavCand { e: ProfileEntry; j: number; agentNo: number; dist: number; x: number; y: number; z: number }

const tmp = new Float64Array(3)
const dir = new Float64Array(3)

function dist2(cam: Float64Array | null, x: number, y: number, z: number): number {
  return cam ? (cam[0] - x) ** 2 + (cam[1] - y) ** 2 + (cam[2] - z) ** 2 : 0
}

/** model index of the volume tables */
export function volumeModelIndex(v: VolumesFrame, model: string | null, profile: ProfileFrame | null): number {
  let m = model ? v.models.indexOf(model) : -1
  if (m < 0 && profile && profile.uavs.length > 0) m = v.models.indexOf(profile.uavs[0].modelId)
  return m < 0 ? (v.models.length > 0 ? 0 : -1) : m
}

/** the selected target's body for a field kind (largest table), used for the vehicles' solid shells */
function shellBody(inp: FieldBuildInput): { body: VolumeBody; row: number } | null {
  const v = inp.volumes
  const t = inp.selectedTarget
  if (!v || !t || !inp.targets || inp.kind === 'none' || inp.kind === 'thermal') return null
  const vt = v.targets.get(t)
  const row = inp.targets.rowOf(t)
  if (!vt || row < 0) return null
  let best: VolumeBody | null = null
  for (const b of vt.bodies) if (bodyModality(b.kind) === inp.kind && (!best || (b.rEffM[0] ? tableRef(b.rEffM[0]) : 0) > (best.rEffM[0] ? tableRef(best.rEffM[0]) : 0))) best = b
  return best ? { body: best, row } : null
}

/**
 * build the instance data into buf; returns its stats. Allocation only for the candidate lists (the call runs at most
 * once per second or on a user action).
 */
export function buildField(inp: FieldBuildInput, buf: FieldBuffers): FieldStats {
  const st = emptyStats(inp.kind)
  buf.n = 0
  buf.nVerts = 0
  buf.anchors.length = 0
  buf.stats = st
  if (inp.level >= FIELD_LEVEL.off) return st
  const B = inp.budget
  const half = inp.level >= FIELD_LEVEL.halfPoints
  // ---------------------------------------------------------------- body anchors
  const bodies: BodyCand[] = []
  const v = inp.volumes
  const T = inp.targets
  const mIdx = v ? volumeModelIndex(v, inp.volumeModel, inp.profile) : -1
  st.modelIdx = mIdx
  if (v && T && mIdx >= 0 && inp.volumesMode !== 'off') {
    const onlySel = inp.volumesMode === 'selected' || inp.level >= FIELD_LEVEL.selectedOnly
    let order = -1
    for (const [id, vt] of v.targets) {
      order++
      if (onlySel && id !== inp.selectedTarget) continue
      const row = T.rowOf(id)
      if (row < 0) continue
      T.pos(row, tmp)
      const yaw = T.yaw(row)
      const d = dist2(inp.camera, tmp[0], tmp[1], tmp[2])
      for (const b of vt.bodies) {
        const cfg = T.body(id, row, b.idx)
        if (!cfg || cfg.enabled === false || !b.rEffM[mIdx] || !SHOWN_BODY.has(b.kind)) continue
        bodies.push({ id, order, row, body: b, cfg, dist: d, yaw })
      }
    }
    if (bodies.length > B.bodies) bodies.sort((a, c) => a.dist - c.dist).length = B.bodies
  }
  // ---------------------------------------------------------------- vehicle anchors
  const uavs: UavCand[] = []
  const f = inp.profile && inp.kind !== 'none' && inp.profile.kind === inp.kind ? inp.profile : null
  if (f) {
    for (let j = 0; j < f.uavs.length; j++) {
      const e = f.uavs[j]
      const a = inp.uavs.agentNo(e.id)
      if (a < 0 || !inp.uavs.pose(a, tmp)) continue
      uavs.push({ e, j, agentNo: a, dist: dist2(inp.camera, tmp[0], tmp[1], tmp[2]), x: tmp[0], y: tmp[1], z: tmp[2] })
    }
    const room = FIELD.anchors - bodies.length
    if (uavs.length > room) uavs.sort((a, c) => a.dist - c.dist).length = Math.max(0, room)
  }
  // ---------------------------------------------------------------- point counts
  const want = new Array<number>(uavs.length + bodies.length)
  for (let i = 0; i < uavs.length; i++) want[i] = half ? B.perUav >> 1 : B.perUav
  for (let i = 0; i < bodies.length; i++) want[uavs.length + i] = half ? B.perBody >> 1 : B.perBody
  const counts = new Array<number>(want.length)
  allocatePoints(want, Math.min(buf.capPoints, half ? B.totalPoints >> 1 : B.totalPoints), counts)
  const [sMin, sMax] = B.sizePx
  // ---------------------------------------------------------------- vehicle points
  const sb = shellBody(inp)
  const sbTarget = new Float64Array(3)
  if (sb && T) T.pos(sb.row, sbTarget)
  const sbYaw = sb && T ? T.yaw(sb.row) : 0
  for (let a = 0; a < uavs.length && f; a++) {
    const u = uavs[a]
    const slot = buf.anchors.length
    const rMax = anchorRMax(f, u.e)
    const n = Math.min(counts[a], PATTERN.maxPerAnchor)
    buf.anchors.push({ kind: 'uav', id: u.e.id, agentNo: u.agentNo, row: -1, rowKey: -1, bodyIdx: -1, j: u.j, points: 0, yaw: 0 })
    st.uavAnchors++
    if (!(rMax > PATTERN.rMinUav)) continue
    const shape = FIELD_SHAPE[f.kind]
    const shellR = solidShell(inp, f, u, sb, sbTarget, sbYaw)
    const highlightShell = inp.kind === 'acoustic' || inp.kind === 'visual'
    let kept = 0
    for (let i = 0; i < n && buf.n < buf.capPoints; i++) {
      const h1 = patternDir(i, u.j, dir)
      const r = patternRadius(h1, PATTERN.rMinUav, rMax)
      const eps = Math.asin(Math.max(-1, Math.min(1, dir[2])))
      const val = profileValue(f, u.e, r, -eps * DEG)
      const band = profileBand(f, val)
      if (band <= 0) continue
      const inside = highlightShell && r <= shellR(eps)
      const ramp = inside ? FIELD.ramp.top : band - 1
      const t = inside ? 1 : (band - 1) / 4
      writePoint(buf, slot, dir, r, pointCode(ramp, shape), FIELD.bandAlpha[0] + t * (FIELD.bandAlpha[1] - FIELD.bandAlpha[0]), inside ? sMax + 1 : sMin + t * (sMax - sMin))
      if (inside) st.inShell++
      kept++
    }
    buf.anchors[slot].points = kept
  }
  // ---------------------------------------------------------------- volume points
  const pos = new Float64Array(3)
  for (let k = 0; k < bodies.length; k++) {
    const c = bodies[k]
    const slot = buf.anchors.length
    const n = Math.min(counts[uavs.length + k], PATTERN.maxPerAnchor)
    const j = volumeAnchorIndex(c.order, c.body.idx)
    buf.anchors.push({ kind: 'body', id: c.id, agentNo: -1, row: c.row, rowKey: T!.keyOf(c.row), bodyIdx: c.body.idx, j, points: 0, yaw: c.yaw })
    st.bodyAnchors++
    const table = c.body.rEffM[mIdx]
    const w = c.body.wM[mIdx] ?? 0
    const ref = tableRef(table)
    const rMax = Math.max(c.cfg.radius_max_m > 0 ? c.cfg.radius_max_m : ref + 2 * w, 2 * PATTERN.rMinBody)
    T!.pos(c.row, pos)
    for (let i = 0; i < n; i++) {
      const h1 = patternDir(i, j, buf.dirs, 3 * i)
      const r = patternRadius(h1, PATTERN.rMinBody, rMax)
      buf.radii[i] = r
      for (let q = 0; q < 3; q++) buf.world[3 * i + q] = pos[q] + buf.dirs[3 * i + q] * r
    }
    bodyMask(c.cfg, pos, c.yaw, buf.world.subarray(0, 3 * n), buf.mask)
    const physical = c.body.mode === 'physical' || inp.physicalRef
    const modality = bodyModality(c.body.kind)
    const code = pointCode(MODAL_RAMP[modality], FIELD_SHAPE[modality])
    const size = (sMin + sMax) / 2
    let kept = 0
    for (let i = 0; i < n && buf.n < buf.capPoints; i++) {
      if (buf.mask[i] !== 1) continue
      const r = buf.radii[i]
      const dx = buf.dirs[3 * i], dy = buf.dirs[3 * i + 1], dz = buf.dirs[3 * i + 2]
      const p = physical ? volumeP(r, volumeREff(table, azRel(dx, dy, c.yaw), Math.asin(Math.max(-1, Math.min(1, dz)))), w, ref) : 1
      dir[0] = dx
      dir[1] = dy
      dir[2] = dz
      writePoint(buf, slot, dir, r, code, FIELD.volAlpha[0] + FIELD.volAlpha[1] * p, size)
      st.pMin = Number.isNaN(st.pMin) ? p : Math.min(st.pMin, p)
      st.pMax = Number.isNaN(st.pMax) ? p : Math.max(st.pMax, p)
      kept++
    }
    buf.anchors[slot].points = kept
  }
  st.points = buf.n
  // ---------------------------------------------------------------- shells
  if (f && uavs.length > 0) buildShells(inp, f, uavs, sb, sbTarget, sbYaw, buf, st)
  return st
}

function writePoint(buf: FieldBuffers, slot: number, d: Float64Array, r: number, code: number, alpha: number, size: number): void {
  const i = buf.n++
  buf.off[3 * i] = d[0] * r
  buf.off[3 * i + 1] = d[1] * r
  buf.off[3 * i + 2] = d[2] * r
  buf.info[4 * i] = slot
  buf.info[4 * i + 1] = code
  buf.info[4 * i + 2] = alpha
  buf.info[4 * i + 3] = size
}

/**
 * radius of the solid shell around vehicle u as a function of eps (elevation of the shell point seen from the vehicle,
 * rad): the selected target's effective radius toward this vehicle (the target sees the vehicle at elevation -eps), else
 * the frame's default shell (person or D level), NaN when unknown
 */
function solidShell(inp: FieldBuildInput, f: ProfileFrame, u: UavCand, sb: { body: VolumeBody; row: number } | null, tp: Float64Array,
  tYaw: number): (eps: number) => number {
  const v = inp.volumes
  const m = v ? v.models.indexOf(u.e.modelId) : -1
  const table = sb && m >= 0 ? sb.body.rEffM[m] : undefined
  if (table) {
    const az = azRel(u.x - tp[0], u.y - tp[1], tYaw)
    return (eps) => volumeREff(table, az, -eps)
  }
  const r = u.e.shellM
  return () => r
}

/** circles of one shell: equator and up to two meridians, radius by elevation */
function buildShells(inp: FieldBuildInput, f: ProfileFrame, uavs: UavCand[], sb: { body: VolumeBody; row: number } | null, tp: Float64Array, tYaw: number,
  buf: FieldBuffers, st: FieldStats): void {
  const iso = inp.level >= FIELD_LEVEL.noDashed ? [] : f.iso
  const cap = Math.min(inp.budget.circles, buf.capCircles)
  const shells = 1 + iso.length
  let perShell = 3
  while (perShell > 1 && uavs.length * shells * perShell > cap) perShell--
  let withIso = iso.length > 0
  if (uavs.length * shells * perShell > cap) withIso = false
  const perUav = perShell * (withIso ? shells : 1)
  const nUav = Math.min(uavs.length, Math.floor(cap / Math.max(1, perUav)))
  // nearest vehicles keep their shells
  const order = uavs.map((_, i) => i).sort((a, b) => uavs[a].dist - uavs[b].dist).slice(0, nUav)
  for (const ui of order) {
    const u = uavs[ui]
    const slot = buf.anchors.findIndex((a) => a.kind === 'uav' && a.id === u.e.id)
    if (slot < 0) continue
    const hl = inp.highlightUav === u.e.id
    const solid = solidShell(inp, f, u, sb, tp, tYaw)
    st.circles += greatCircles(buf, slot, solid, perShell, -1, hl ? FIELD.ramp.highlight : FIELD.ramp.solid, hl ? 1 : FIELD.shellAlpha)
    if (!withIso) continue
    for (const s of iso) {
      const fn = (eps: number): number => isoRadius(f, u.e, s.v, -eps * DEG)
      st.circles += greatCircles(buf, slot, fn, perShell, FIELD.dashesPerCircle, FIELD.ramp.iso, FIELD.isoAlpha)
    }
  }
  st.segments = buf.nVerts / 2
}

const MERIDIANS = [0, Math.PI / 2]
const ca = new Float64Array(3)
const cb = new Float64Array(3)

/** up to `count` circles (equator, meridian 0, meridian 90 deg) of a shell; dashes > 0: dashed; returns the circles written */
function greatCircles(buf: FieldBuffers, slot: number, radius: (eps: number) => number, count: number, dashes: number, ramp: number, alpha: number): number {
  const N = FIELD.circleSegs
  let written = 0
  for (let c = 0; c < count; c++) {
    if (buf.nVerts + 2 * N > buf.capVerts) break
    let any = false
    for (let k = 0; k < N; k++) {
      if (!circlePoint(c, k / N, radius, ca) || !circlePoint(c, (k + 1) / N, radius, cb)) continue
      const d0 = dashes > 0 ? (k / N) * dashes : -1
      const d1 = dashes > 0 ? ((k + 1) / N) * dashes : -1
      writeVert(buf, slot, ca, ramp, d0, alpha)
      writeVert(buf, slot, cb, ramp, d1, alpha)
      any = true
    }
    if (any) written++
  }
  return written
}

/** point at fraction t of circle c (0 equator, 1 and 2 meridians); false when the radius is unknown there */
function circlePoint(c: number, t: number, radius: (eps: number) => number, out: Float64Array): boolean {
  const a = 2 * Math.PI * t
  let x: number, y: number, z: number
  if (c === 0) {
    x = Math.cos(a)
    y = Math.sin(a)
    z = 0
  } else {
    const ph = MERIDIANS[c - 1]
    x = Math.cos(a) * Math.cos(ph)
    y = Math.cos(a) * Math.sin(ph)
    z = Math.sin(a)
  }
  const r = radius(Math.asin(Math.max(-1, Math.min(1, z))))
  if (!(r > 0) || !Number.isFinite(r)) return false
  out[0] = x * r
  out[1] = y * r
  out[2] = z * r
  return true
}

function writeVert(buf: FieldBuffers, slot: number, p: Float64Array, ramp: number, dash: number, alpha: number): void {
  const i = buf.nVerts++
  buf.sPos[3 * i] = p[0]
  buf.sPos[3 * i + 1] = p[1]
  buf.sPos[3 * i + 2] = p[2]
  buf.sInfo[4 * i] = slot
  buf.sInfo[4 * i + 1] = ramp
  buf.sInfo[4 * i + 2] = dash
  buf.sInfo[4 * i + 3] = alpha
}
