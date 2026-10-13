// Thermal emitters of the LWIR view (D2 WP-11; AWR-04 §6.2 LWIR row, §7.2 thermal attributes; ADR-108). Owner: M06.
// Per frame while the LWIR view is shown: the registered sources (M18: targets by class temperature, emissivity and hot
// fraction) and the other vehicles of the drone layer (motors and batteries, ir.motor_delta_t_k of the model) give boxes
// in ENU; each is projected with the FPV camera (8 corners of the yawed box -> NDC rectangle), its apparent temperature
// computed (emissivity mix with the sky, path transmittance to the air temperature), the nearest SV.maxEmitters kept and
// written far to near into the emitter table of the LWIR material (row i: [cx, cy, hx, hy] NDC, [near range m, T core,
// T shell, core scale], [range m, 0, 0, 0]). Zero allocation per frame.
import { Vector3, type PerspectiveCamera } from 'three'
import { rtClient } from '@/net/rt'
import { shapeOf } from '@/engine'
import { vp } from '../session'
import { EMIT_COLS } from './materials'
import { pathApparentC, surfaceApparentC, SV } from './math'
import { EMITTER as E, EMITTER_STRIDE, sv, type SensorEnv } from './state'

const MAX_RAW = 96
const raw = new Float64Array(MAX_RAW * EMITTER_STRIDE)
const scratch = new Float64Array(MAX_RAW * EMITTER_STRIDE)
const dist = new Float64Array(MAX_RAW)
const order = new Int32Array(MAX_RAW)
const rect = new Float64Array(MAX_RAW * 4)
const ok = new Uint8Array(MAX_RAW)
const v = new Vector3()
const quat = new Float64Array(4)
const posA = new Float64Array(3)
const velA = new Float64Array(3)

/** count: rows written; candidates: records collected (sources + vehicles); inView: of those, in front and inside the view */
export interface EmitterFrame { count: number; hotC: number; candidates: number; inView: number }
const result: EmitterFrame = { count: 0, hotC: Number.NaN, candidates: 0, inView: 0 }

/** project the yawed box of record i to an NDC rectangle (rect[4i..4i+3] = min x, min y, max x, max y); false when behind */
function project(i: number, cam: PerspectiveCamera): boolean {
  const o = i * EMITTER_STRIDE
  const cx = raw[o + E.X], cy = raw[o + E.Y], cz = raw[o + E.Z]
  const hl = raw[o + E.L] / 2, hw = raw[o + E.W] / 2, hh = raw[o + E.H] / 2
  const c = Math.cos(raw[o + E.YAW]), s = Math.sin(raw[o + E.YAW])
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  let front = 0
  for (let k = 0; k < 8; k++) {
    const a = (k & 1 ? 1 : -1) * hl, b = (k & 2 ? 1 : -1) * hw, h = (k & 4 ? 1 : -1) * hh
    const e = cx + a * c - b * s
    const n = cy + a * s + b * c
    const u = cz + h
    v.set(e, u, -n).applyMatrix4(cam.matrixWorldInverse)
    if (v.z > -cam.near) continue
    front++
    v.applyMatrix4(cam.projectionMatrix)
    if (v.x < x0) x0 = v.x
    if (v.x > x1) x1 = v.x
    if (v.y < y0) y0 = v.y
    if (v.y > y1) y1 = v.y
  }
  if (front === 0 || x1 < -1.05 || x0 > 1.05 || y1 < -1.05 || y0 > 1.05) return false
  rect[4 * i] = x0
  rect[4 * i + 1] = y0
  rect[4 * i + 2] = x1
  rect[4 * i + 3] = y1
  return true
}

function addDrones(n0: number, env: SensorEnv): number {
  let n = n0
  const d = vp.drones
  const rig = vp.rig
  if (!d) return n
  const focus = rig && rig.mode === 'fpv' ? rig.focusAgent : -1
  const roster = rtClient()?.roster
  const p = d.poses
  for (let i = 0; i < p.n && n < MAX_RAW; i++) {
    const a = p.agentNo[i]
    if (a === focus) continue
    const e = roster?.get(a)
    if (e && e.kind !== 'uav' && e.kind !== '') continue
    const sh = shapeOf(e?.model ?? 'p600')
    if (!d.fullPoseOf(a, posA, quat, velA)) continue
    const o = n * EMITTER_STRIDE
    const x = quat[0], y = quat[1], z = quat[2], w = quat[3]
    raw[o + E.X] = posA[0]
    raw[o + E.Y] = posA[1]
    raw[o + E.Z] = posA[2]
    raw[o + E.L] = sh.sizeM[0]
    raw[o + E.W] = sh.sizeM[1]
    raw[o + E.H] = Math.max(0.1, sh.sizeM[2])
    raw[o + E.YAW] = Math.atan2(2 * (x * y + w * z), 1 - 2 * (y * y + z * z))
    raw[o + E.TC] = Number.NaN
    raw[o + E.DT] = sh.motorDeltaTK
    raw[o + E.EPS] = 0.9
    raw[o + E.HOT] = 0.35
    void env
    n++
  }
  return n
}

/**
 * collect, project, rate and write the emitters of this frame into data (EMIT_COLS x SV.maxEmitters RGBA32F texels);
 * returns the count written and the hottest apparent core temperature (NaN when none)
 */
export function writeEmitters(cam: PerspectiveCamera, env: SensorEnv, data: Float32Array): EmitterFrame {
  let n = 0
  const list = sv.sourceList
  for (let s = 0; s < list.length && n < MAX_RAW; s++) {
    let k = 0
    try {
      k = Math.max(0, Math.min(MAX_RAW - n, list[s](scratch, MAX_RAW - n) | 0))
    } catch {
      k = 0
    }
    const base = n * EMITTER_STRIDE
    for (let c = 0; c < k * EMITTER_STRIDE; c++) raw[base + c] = scratch[c]
    n += k
  }
  n = addDrones(n, env)
  const cpx = cam.position.x, cpy = cam.position.y, cpz = cam.position.z
  let m = 0
  for (let i = 0; i < n; i++) {
    const o = i * EMITTER_STRIDE
    ok[i] = project(i, cam) ? 1 : 0
    if (!ok[i]) continue
    // camera position in the three frame (E, U, -N)
    dist[i] = Math.hypot(raw[o + E.X] - cpx, raw[o + E.Z] - cpy, -raw[o + E.Y] - cpz)
    order[m++] = i
  }
  // nearest first (insertion sort: a few dozen at most)
  for (let a = 1; a < m; a++) {
    const t = order[a]
    let b = a - 1
    while (b >= 0 && dist[order[b]] > dist[t]) {
      order[b + 1] = order[b]
      b--
    }
    order[b + 1] = t
  }
  const keep = Math.min(m, SV.maxEmitters)
  const tBg = env.tSurfaceC
  let hot = Number.NaN
  for (let j = 0; j < keep; j++) {
    const i = order[keep - 1 - j] // far to near: nearer emitters overwrite
    const o = i * EMITTER_STRIDE
    const r = dist[i]
    const tObj = Number.isFinite(raw[o + E.TC]) ? raw[o + E.TC] : tBg + raw[o + E.DT]
    const eps = Number.isFinite(raw[o + E.EPS]) ? raw[o + E.EPS] : 0.95
    const hf = Number.isFinite(raw[o + E.HOT]) ? raw[o + E.HOT] : 1
    const core = pathApparentC(surfaceApparentC(tObj, eps, env.tSkyC), env.tAirC, env.sigmaLwPerM, r)
    const shellObj = hf >= 1 ? tObj : tBg + 0.35 * (tObj - tBg)
    const shell = pathApparentC(surfaceApparentC(shellObj, eps, env.tSkyC), env.tAirC, env.sigmaLwPerM, r)
    const halfDiag = 0.5 * Math.hypot(raw[o + E.L], raw[o + E.W], raw[o + E.H])
    const q = j * EMIT_COLS * 4
    const x0 = rect[4 * i], y0 = rect[4 * i + 1], x1 = rect[4 * i + 2], y1 = rect[4 * i + 3]
    data[q] = (x0 + x1) / 2
    data[q + 1] = (y0 + y1) / 2
    data[q + 2] = (x1 - x0) / 2
    data[q + 3] = (y1 - y0) / 2
    data[q + 4] = r - halfDiag - 0.5
    data[q + 5] = core
    data[q + 6] = shell
    data[q + 7] = hf >= 1 ? 1 : Math.sqrt(Math.max(hf, 0.25))
    data[q + 8] = r
    data[q + 9] = 0
    data[q + 10] = 0
    data[q + 11] = 0
    if (!(core <= hot)) hot = core
  }
  result.count = keep
  result.hotC = hot
  result.candidates = n
  result.inView = m
  return result
}
