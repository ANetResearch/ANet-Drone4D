// Download candidates (M05 §6.5.2, M05-FR-014, FR-016..018, FR-020). Owner: M05.
// Candidates are the selected nodes in pop order that are neither resident, cached, in flight nor waiting for a retry
// (a FAILED node returns to UNLOADED with its attempts cleared 10 s after the third failure). In Follow/FPV mode only the
// first 2 x maxInflight candidates are re-ordered by key x w_center x w_focus (w_center = clamp(1 - |ndc|, 0, 1) + 0.5,
// w_focus = 1 + 1.5 exp(-|c - p_focus|^2 / (2 x 150^2))); the weights never enter the selection key, so the selection
// stays monotone in B. The prefetch queue (D1-ext) holds up to 64 nodes of a camera-flight destination and is drained
// only while fewer than half of the fetch slots are busy.
import { NS, type NodeStore } from './NodeStore'
import type { Selection } from './Selector'
import { toNdc, type LodCamera } from './frustum'
import { PC } from '../params'
import { hypot2 } from '../../hypot'

export interface Candidates { n: number; node: Int32Array; key: Float64Array }
export const newCandidates = (cap: number): Candidates => ({ n: 0, node: new Int32Array(cap), key: new Float64Array(cap) })

export interface FocusState { mode: 'none' | 'follow' | 'fpv'; p: Float64Array }

/** true when node i may be requested now (UNLOADED, or its retry or FAILED wait is over) */
export function requestable(t: NodeStore, i: number, nowMs: number, inFlight: boolean): boolean {
  if (t.numPoints[i] === 0 || t.poolBase[i] >= 0 || t.cacheSlot[i] >= 0 || inFlight) return false
  const s = t.state[i]
  if (s === NS.RETRY_WAIT || s === NS.FAILED) {
    if (nowMs < t.retryAt[i]) return false
    if (s === NS.FAILED) t.attempts[i] = 0
    t.state[i] = NS.UNLOADED
  }
  return true
}

export function collectCandidates(sel: Selection, t: NodeStore, nowMs: number, reqOfNode: Int32Array, out: Candidates): Candidates {
  let m = 0
  for (let k = 0; k < sel.n; k++) {
    const i = sel.idx[k]
    if (!requestable(t, i, nowMs, reqOfNode[i] >= 0)) continue
    out.node[m] = i
    out.key[m] = sel.key[k]
    m++
  }
  out.n = m
  return out
}

const NDC = new Float64Array(2)
const SCORE = new Float64Array(64)

/** reorder the first `window` candidates by key x w_center x w_focus (stable insertion sort, no allocation) */
export function reorderWindow(c: Candidates, window: number, t: NodeStore, cam: LodCamera, focus: FocusState): void {
  const m = Math.min(c.n, window, SCORE.length)
  const s2 = 2 * PC.focusSigmaM * PC.focusSigmaM
  for (let k = 0; k < m; k++) {
    const i = c.node[k]
    const o = 3 * i
    const cx = 0.5 * (t.tightMin[o] + t.tightMax[o])
    const cy = 0.5 * (t.tightMin[o + 1] + t.tightMax[o + 1])
    const cz = 0.5 * (t.tightMin[o + 2] + t.tightMax[o + 2])
    let wc = 0.5
    if (toNdc(cam, cx, cy, cz, NDC)) wc = Math.min(Math.max(1 - hypot2(NDC[0], NDC[1]), 0), 1) + 0.5
    const dx = cx - focus.p[0]
    const dy = cy - focus.p[1]
    const dz = cz - focus.p[2]
    const wf = 1 + PC.focusGain * Math.exp(-(dx * dx + dy * dy + dz * dz) / s2)
    SCORE[k] = c.key[k] * wc * wf
  }
  for (let a = 1; a < m; a++) {
    const sn = c.node[a]
    const sk = c.key[a]
    const sv = SCORE[a]
    let b = a - 1
    while (b >= 0 && SCORE[b] < sv) {
      c.node[b + 1] = c.node[b]
      c.key[b + 1] = c.key[b]
      SCORE[b + 1] = SCORE[b]
      b--
    }
    c.node[b + 1] = sn
    c.key[b + 1] = sk
    SCORE[b + 1] = sv
  }
}

/** low-priority queue of a camera-flight destination (D1-ext, M05-FR-020) */
export class PrefetchQueue {
  readonly node = new Int32Array(PC.prefetchMax)
  n = 0
  head = 0
  clear(): void {
    this.n = 0
    this.head = 0
  }
  fill(sel: Selection, t: NodeStore, nowMs: number, reqOfNode: Int32Array): void {
    this.clear()
    for (let k = 0; k < sel.n && this.n < this.node.length; k++) {
      const i = sel.idx[k]
      if (requestable(t, i, nowMs, reqOfNode[i] >= 0)) this.node[this.n++] = i
    }
  }
  /** next node still requestable, or -1 */
  next(t: NodeStore, nowMs: number, reqOfNode: Int32Array): number {
    while (this.head < this.n) {
      const i = this.node[this.head++]
      if (requestable(t, i, nowMs, reqOfNode[i] >= 0)) return i
    }
    return -1
  }
}
