// GPU eviction plan (M05 §6.6.4, M05-FR-024, FR-026; port of openlidarviewer evictionPolicy.ts planEviction to typed
// arrays, with the budget replaced by B_ref = the current rung's clamped hi). Owner: M05. Pure: no DOM, no three, no
// clock, no randomness; the same candidate set gives the same plan in any input order; zero allocation per call.
//
// Nothing is evicted while residentPts <= 1.5 B_ref (the hysteresis band that stops boundary nodes pulsing); past it,
// candidates are taken in one priority order until the resident total reaches 1.15 B_ref. With `force` (the page
// allocator failed) the band is ignored and at least needPts are released. Candidates are the GPU-resident nodes that
// are not in this frame's selection and are not roots. Classes, cheapest first: 0 outside the frustum, 1 in view but not
// selected, 2 in view, not selected and resident for less than the 1 s dwell (the dwell moves a node down the order, it
// never removes it: hysteresis is a preference, never a veto). Within a class: deeper first, then farther, then the
// node index. The "superseded by finer detail" class of OLV is empty for additive LOD (parents are always selected
// before their children); it is kept for the V0.8 REPLACE layers.
import { PC } from '../params'
import { sortIdx } from './heap'

export interface EvictionCandidates {
  n: number
  node: Int32Array
  pts: Int32Array
  level: Int32Array
  dist: Float64Array
  outside: Uint8Array
  residentSince: Float64Array
  /** allocation failed: ignore the band and release at least needPts */
  force: boolean
  needPts: number
}

export function newEvictionCandidates(cap: number): EvictionCandidates {
  return {
    n: 0, node: new Int32Array(cap), pts: new Int32Array(cap), level: new Int32Array(cap), dist: new Float64Array(cap), outside: new Uint8Array(cap),
    residentSince: new Float64Array(cap), force: false, needPts: 0,
  }
}

export const CLASS_OUTSIDE = 0
export const CLASS_VIEW = 1
export const CLASS_DWELL = 2

let E: EvictionCandidates | null = null
let CLS: Uint8Array = new Uint8Array(0)
let ORDER: Int32Array = new Int32Array(0)

function cmp(a: number, b: number): number {
  const c = E!
  return CLS[a] - CLS[b] || c.level[b] - c.level[a] || c.dist[b] - c.dist[a] || c.node[a] - c.node[b]
}

/** eviction class of candidate k (M05 §6.6.4) */
export function evictionClass(c: EvictionCandidates, k: number, nowMs: number, dwellMs: number = PC.dwellMs): number {
  if (c.outside[k]) return CLASS_OUTSIDE
  const age = nowMs - c.residentSince[k]
  // a non-finite or future timestamp reads as "just arrived" (OLV isDwellProtected)
  return !Number.isFinite(age) || age < dwellMs ? CLASS_DWELL : CLASS_VIEW
}

/**
 * Plan one eviction: writes node indices into out in drop order and returns their count (0 inside the band).
 * Bref is the current rung's clamped hi (min(rung.hi, 0.6 x pool)), not the CAS B (ADR-010).
 */
export function planEviction(c: EvictionCandidates, residentPts: number, Bref: number, nowMs: number, out: Int32Array, dwellMs: number = PC.dwellMs): number {
  if (!c.force && residentPts <= PC.evictTrigger * Bref) return 0
  const target = c.force ? residentPts - Math.max(0, c.needPts) : PC.evictTo * Bref
  const n = c.n
  if (CLS.length < n) {
    CLS = new Uint8Array(Math.max(n, 2 * CLS.length))
    ORDER = new Int32Array(CLS.length)
  }
  for (let k = 0; k < n; k++) {
    CLS[k] = evictionClass(c, k, nowMs, dwellMs)
    ORDER[k] = k
  }
  E = c
  sortIdx(ORDER, n, cmp)
  E = null
  let remaining = residentPts
  let m = 0
  for (let j = 0; j < n && remaining > target; j++) {
    const k = ORDER[j]
    out[m++] = c.node[k]
    remaining -= Math.max(0, c.pts[k])
  }
  return m
}
