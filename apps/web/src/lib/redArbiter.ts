// One-red arbitration (M15-FR-091; AWR-15 §3.7.2; ADR-032). Pure function shared by the viewport (M06
// viewport/bindings/redOwner.ts -> drones.setRedOwner) and every DOM figure (data-figure root). One RedState per figure,
// evaluated every INPUT.redEvalIntervalMs (250 ms, wall clock). Priority: unacknowledged critical (highest rank, then
// latest) > focused vehicle (selected) > data hero. Critical and hero owners dwell INPUT.redDwellMs (1.5 s) against
// candidates of the same level and the same or lower rank; a higher level always pre-empts; selected switches at once.
// Entities are compared by key (kind, id): candidates are rebuilt every evaluation.
import { INPUT } from './tokens/input.gen'

export type RedLevel = 'critical' | 'selected' | 'hero'
export interface RedEntity {
  kind: 'drone' | 'zone' | 'target' | 'class' | 'row' | 'series' | 'point' | 'cell'
  id: string
}
export interface RedCandidate {
  entity: RedEntity
  level: RedLevel
  /** severity rank, larger is more severe; only meaningful for critical (others 0) */
  rank: number
  /** wall clock ms of the latest trigger (selected and hero: when they became candidates) */
  tLastWallMs: number
}
export interface RedState {
  owner: RedEntity | null
  level: RedLevel | null
  ownerSinceMs: number
}

export const RED_NONE: RedState = Object.freeze({ owner: null, level: null, ownerSinceMs: 0 }) as RedState
const LV: Readonly<Record<RedLevel, number>> = { critical: 2, selected: 1, hero: 0 }
export const sameEntity = (a: RedEntity | null, b: RedEntity | null): boolean => !!a && !!b && a.kind === b.kind && a.id === b.id

export function arbitrate(prev: RedState, cands: readonly RedCandidate[], nowMs: number): RedState {
  let pick: RedCandidate | null = null
  for (let i = 0; i < cands.length; i++) {
    const c = cands[i]
    if (!pick || LV[c.level] > LV[pick.level] ||
        (c.level === pick.level && (c.rank > pick.rank || (c.rank === pick.rank && c.tLastWallMs > pick.tLastWallMs)))) pick = c
  }
  let cur: RedCandidate | null = null
  for (let i = 0; i < cands.length; i++) {
    const c = cands[i]
    if (sameEntity(c.entity, prev.owner) && c.level === prev.level) {
      cur = c
      break
    }
  }
  if (cur && pick && pick.level === cur.level && cur.level !== 'selected' &&
      pick.rank <= cur.rank && nowMs - prev.ownerSinceMs < INPUT.redDwellMs) return prev
  if (!pick) return prev.owner ? { owner: null, level: null, ownerSinceMs: nowMs } : prev
  if (sameEntity(pick.entity, prev.owner) && pick.level === prev.level) return prev
  return { owner: pick.entity, level: pick.level, ownerSinceMs: nowMs }
}
