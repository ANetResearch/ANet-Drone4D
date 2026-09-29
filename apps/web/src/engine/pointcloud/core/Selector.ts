// APH selector (ADR-009; M05 §6.4, M05-FR-010..013; product port of g02 lod.mjs selA(prefix = true, hyst = 0.1)).
// Owner: M05. Best-first over all roots (one heap, one budget) by the screen-space error in raster px
//   key = spacing_L x (0.5 H_px) / (tan(fovY/2) x max(d, near)),  d = eye to the node's subtree tight AABB
// Required nodes (key >= tau) may fill B; bonus nodes (tau/4 <= key < tau) fill up to floor(B (1 - headroom)).
// A rejected node is skipped (not a break) up to maxSkips; the first rejected required node is drawn as a prefix
// cnt = B - pts (>= minPrefix) whose children are not expanded. Children are culled when pushed; a child drawn last
// frame (selected and resident, t.drawnFrame written by the DrawTable builder) gets key x 1.1, others x 0.9.
// Semantic differences to the prototype (M05 §14 items 9 and 10): roots are culled and keyed by their real error, only
// the first admitted node is exempt from the budget (a prefix of B when own > B), and the hysteresis mark comes from
// the DrawTable (selected and resident) instead of every selected node.
// Pure: no DOM, no three, no clock; zero allocation per call.
import type { NodeStore } from './NodeStore'
import { NodeHeap } from './heap'
import { INTERSECT, OUTSIDE, classifyNode, distToBox, type LodCamera } from './frustum'

export interface SelectOptions {
  tau: number
  B: number
  headroom: number
  maxNodes: number
  maxSkips: number
  minPrefix: number
  hysteresis: number
  /** level cap (255 = none); the first-frame target set uses the first-screen level L (M05-FR-004) */
  depthCap: number
  tauMinFrac: number
}

export interface LodScratch {
  heap: NodeHeap
  /** selection counter; Selection.frame and NodeStore.drawnFrame use it */
  frame: number
}

export interface Selection {
  idx: Int32Array
  cnt: Int32Array
  key: Float32Array
  n: number
  points: number
  frame: number
  /** 0 budget, 1 nodes, 2 headroom, 3 error, 4 complete (__perf.pc.limitedBy encoding) */
  limitedBy: 0 | 1 | 2 | 3 | 4
  /** largest error left in the abandoned region, raster px */
  achieved: number
  /** smallest spacing among admitted nodes, m (M06 near plane) */
  minSpacingM: number
  /** selected points per level */
  levelCounts: Int32Array
}

export const LIMITED = ['budget', 'nodes', 'headroom', 'error', 'complete'] as const
export const LB_BUDGET = 0
export const LB_NODES = 1
export const LB_HEADROOM = 2
export const LB_ERROR = 3
export const LB_COMPLETE = 4

export function newScratch(N: number): LodScratch {
  return { heap: new NodeHeap(2 * N + 8), frame: 0 }
}
export function newSelection(maxNodes: number): Selection {
  return {
    idx: new Int32Array(maxNodes), cnt: new Int32Array(maxNodes), key: new Float32Array(maxNodes), n: 0, points: 0, frame: 0, limitedBy: 4,
    achieved: 0, minSpacingM: 0, levelCounts: new Int32Array(32),
  }
}

/** screen-space error of node i in raster px (M05-FR-011) */
export function keyPx(t: NodeStore, cam: LodCamera, i: number): number {
  if (cam.orthoH > 0) return (t.spacing[i] * cam.hPx) / cam.orthoH
  const d = Math.max(distToBox(t.tightMin, t.tightMax, i, cam.eye), cam.near)
  return (t.spacing[i] * (0.5 * cam.hPx)) / (cam.slope * d)
}

const F_BUDGET = 1
const F_NODES = 2
const F_HEADROOM = 4
const F_FLOOR = 8

export function selectVisible(t: NodeStore, cam: LodCamera, o: SelectOptions, s: LodScratch, out: Selection): Selection {
  const frame = ++s.frame
  const heap = s.heap
  const tau = o.tau
  const tauMin = tau * o.tauMinFrac
  const B = o.B
  const bonus = Math.floor(B * (1 - o.headroom))
  const pl = cam.planes
  const drawn = t.drawnFrame
  const hUp = 1 + o.hysteresis
  const hDown = 1 - o.hysteresis
  heap.clear()
  out.levelCounts.fill(0)
  // M05-FR-012: roots are culled and keyed by their real error (the prototype pushes +inf); pushed as "intersecting"
  // so their children are always classified
  for (let r = 0; r < t.roots.length; r++) {
    const i0 = t.roots[r]
    if (classifyNode(pl, t.tightMin, t.tightMax, i0) !== OUTSIDE) heap.push(i0, keyPx(t, cam, i0), INTERSECT)
  }
  let n = 0
  let pts = 0
  let skips = 0
  let worst = 0
  let flags = 0
  let prefixed = false
  let minSp = Number.POSITIVE_INFINITY
  while (heap.size > 0) {
    if (n >= o.maxNodes) {
      flags |= F_NODES
      break
    }
    heap.pop()
    const i = heap.pn
    const key = heap.pk
    const cont = heap.pc
    const required = key >= tau
    const cap = required ? B : bonus
    const own = t.numPoints[i]
    if (n === 0 && own > B) {
      // first admitted node larger than the whole budget: draw a prefix of B and do not expand (M05-FR-012)
      out.idx[0] = i
      out.cnt[0] = B
      out.key[0] = key
      out.levelCounts[t.level[i]] += B
      n = 1
      pts = B
      flags |= F_BUDGET
      if (key > worst) worst = key
      if (t.spacing[i] < minSp) minSp = t.spacing[i]
      continue
    }
    if (n > 0 && pts + own > cap) {
      flags |= required ? F_BUDGET : F_HEADROOM
      if (key > worst) worst = key
      if (required && !prefixed && B - pts >= o.minPrefix) {
        // the first rejected required node is drawn as a prefix; its children are not expanded
        prefixed = true
        out.idx[n] = i
        out.cnt[n] = B - pts
        out.key[n] = key
        out.levelCounts[t.level[i]] += B - pts
        n++
        pts = B
        if (t.spacing[i] < minSp) minSp = t.spacing[i]
        continue
      }
      if (++skips > o.maxSkips) break
      continue
    }
    pts += own
    out.idx[n] = i
    out.cnt[n] = own
    out.key[n] = key
    out.levelCounts[t.level[i]] += own
    n++
    if (t.spacing[i] < minSp) minSp = t.spacing[i]
    if (t.level[i] >= o.depthCap) continue
    for (let c = 0; c < 8; c++) {
      const ch = t.children[8 * i + c]
      if (ch < 0) continue
      const cc = cont === 2 ? 2 : classifyNode(pl, t.tightMin, t.tightMax, ch)
      if (cc === OUTSIDE) continue
      let ck = keyPx(t, cam, ch)
      if (o.hysteresis) ck *= drawn[ch] === frame - 1 ? hUp : hDown
      if (ck < tauMin) {
        flags |= F_FLOOR
        if (ck > worst) worst = ck
        continue
      }
      if (ck < tau && pts >= bonus) {
        flags |= F_HEADROOM
        if (ck > worst) worst = ck
        continue
      }
      heap.push(ch, ck, cc)
    }
  }
  if (heap.size > 0 && heap.topKey > worst) worst = heap.topKey
  out.n = n
  out.points = pts
  out.frame = frame
  out.achieved = worst
  out.minSpacingM = n > 0 ? minSp : 0
  out.limitedBy = flags & F_NODES ? 1 : flags & F_BUDGET ? 0 : flags & F_HEADROOM ? 2 : flags & F_FLOOR ? 3 : 4
  return out
}
