// First-screen level, rule R (ADR-013; AWR-16 §4.11; M05 §6.3). Owner: M05. Pure function.
import { PC } from '../params'

export interface RootLevels { levelsPoints: readonly number[]; levelsByteEnd: readonly number[]; depth: number }

/** deepest level L with sum over roots of levelsPoints[L] <= min(4.5e5, 0.8 x pool, 2.5 x B_hi(start rung)); at least 0 */
export function firstScreenLevel(roots: readonly RootLevels[], poolCapPts: number, startHi: number): number {
  const cap = Math.min(PC.firstScreenAbsCap, PC.firstScreenPoolFrac * poolCapPts, PC.firstScreenHiFactor * startHi)
  let depth = 0
  for (const r of roots) depth = Math.max(depth, r.depth)
  let best = 0
  for (let L = 0; L <= depth; L++) {
    let P = 0
    for (const r of roots) P += r.levelsPoints[Math.min(L, r.levelsPoints.length - 1)]
    if (P <= cap) best = L
    else break
  }
  return best
}

/** bytes of the first-screen Range of one root: [0, levelsByteEnd[L]) */
export function firstScreenBytes(r: RootLevels, L: number): number {
  return r.levelsByteEnd[Math.min(L, r.levelsByteEnd.length - 1)]
}
