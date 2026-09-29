// Opened world snapshot (M05 §7.5, §8.1; M05-FR-005, FR-052). Owner: M05. Read-only for the UI; written at most 4 Hz by
// the point cloud layer adapter (viewport/layers/pointcloud.tsx) from PointCloudStats, and only when a field changed.
// This module does not import the engine (AWR-03 §4.2): the adapter passes plain values.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'

export type WorldPhase = 'idle' | 'manifest' | 'first_screen' | 'streaming' | 'suspended' | 'error'
export type AnchorKind = 'rtk' | 'survey' | 'gnss' | 'synthetic' | 'geodetic'
export type LimitedByName = 'budget' | 'nodes' | 'headroom' | 'error' | 'complete'

export interface WorldState {
  worldId: string | null
  contentVersion: string | null
  phase: WorldPhase
  error: { code: number; message: string } | null
  anchorKind: AnchorKind | null
  /** coordinate.trueNorth.confidence (assumed, unknown, measured ...) and render.syntheticGroundZ (INT-1, M16-FR-006) */
  northConfidence: string | null
  syntheticGroundZ: number | null
  /** coverage 0-1: first-frame target set while opening, then the selection (M05 §3.3) */
  progress: number
  firstScreenBytes: number
  rung: { index: number; name: string; manual: boolean }
  /** CAS budget and its effective band (points) */
  B: number
  Beff: number
  Bfloor: number
  lo: number
  hi: number
  /** points drawn this frame (HUD "points x / budget y") */
  drawn: number
  limitedBy: LimitedByName
  achievedErrPx: number
  fillRate: number
  clampedByCapacity: boolean
  floorHeld: boolean
  inflight: number
  queued: number
  failed: number
  canceled: number
  residentPts: number
  cpuCacheBytes: number
  pendingUploadPts: number
  poolStalls: number
  pageUtil: number
  maxPxEff: number
  rsEff: number
  /** CAS freeze mask: 1 external (hidden, modal, frame cap), 2 shader compile, 4 warm-up */
  frozenMask: number
  /** selected points per level (Perf panel LfRungBars) */
  levelCounts: number[]
  ttfpMs: number | null
  switchMs: number | null
}

export const worldStore = createAwrStore<WorldState>('world', () => ({
  worldId: null, contentVersion: null, phase: 'idle', error: null, anchorKind: null, northConfidence: null, syntheticGroundZ: null, progress: 0, firstScreenBytes: 0,
  rung: { index: 0, name: 'soft-min', manual: false }, B: 0, Beff: 0, Bfloor: 0, lo: 0, hi: 0, drawn: 0, limitedBy: 'complete', achievedErrPx: 0, fillRate: 0,
  clampedByCapacity: false, floorHeld: false, inflight: 0, queued: 0, failed: 0, canceled: 0, residentPts: 0, cpuCacheBytes: 0, pendingUploadPts: 0,
  poolStalls: 0, pageUtil: 0, maxPxEff: 0, rsEff: 1, frozenMask: 0, levelCounts: [], ttfpMs: null, switchMs: null,
}))

/** scalar fields copied from the engine statistics (plain values; the adapter reads them from PointCloudStats) */
export interface WorldStatsInput {
  phase: WorldPhase
  progress: number
  drawn: number
  B: number
  Beff: number
  Bfloor: number
  lo: number
  hi: number
  rungIndex: number
  rungName: string
  manual: boolean
  limitedBy: LimitedByName
  achievedErrPx: number
  fillRate: number
  clampedByCapacity: boolean
  floorHeld: boolean
  inflight: number
  queued: number
  failed: number
  canceled: number
  residentPts: number
  cpuCacheBytes: number
  pendingUploadPts: number
  poolStalls: number
  pageUtil: number
  maxPxEff: number
  rsEff: number
  frozenMask: number
  levelCounts: ArrayLike<number>
  error: { code: number; message: string } | null
}

const round = (x: number, q: number): number => Math.round(x / q) * q

/** one 4 Hz write of the engine statistics; returns false when nothing changed (no store write) */
export function writeWorldStats(s: WorldStatsInput): boolean {
  const cur = worldStore.getState()
  const levels: number[] = []
  let last = 0
  for (let i = 0; i < s.levelCounts.length; i++) if (s.levelCounts[i] > 0) last = i + 1
  for (let i = 0; i < last; i++) levels.push(s.levelCounts[i])
  const next = {
    phase: s.phase, progress: round(s.progress, 0.001), drawn: s.drawn, B: Math.round(s.B), Beff: Math.round(s.Beff), Bfloor: s.Bfloor, lo: Math.round(s.lo),
    hi: Math.round(s.hi), limitedBy: s.limitedBy, achievedErrPx: round(s.achievedErrPx, 0.01), fillRate: round(s.fillRate, 0.001),
    clampedByCapacity: s.clampedByCapacity, floorHeld: s.floorHeld, inflight: s.inflight, queued: s.queued, failed: s.failed, canceled: s.canceled,
    residentPts: s.residentPts, cpuCacheBytes: s.cpuCacheBytes, pendingUploadPts: s.pendingUploadPts, poolStalls: s.poolStalls, pageUtil: round(s.pageUtil, 0.001),
    maxPxEff: round(s.maxPxEff, 0.1), rsEff: s.rsEff, frozenMask: s.frozenMask, error: s.error,
  }
  let changed = cur.rung.index !== s.rungIndex || cur.rung.manual !== s.manual || cur.levelCounts.length !== levels.length
  for (let i = 0; !changed && i < levels.length; i++) if (cur.levelCounts[i] !== levels[i]) changed = true
  for (const k of Object.keys(next) as (keyof typeof next)[]) if (!changed && cur[k] !== next[k]) changed = true
  if (!changed) return false
  worldStore.setState({ ...next, levelCounts: levels, rung: { index: s.rungIndex, name: s.rungName, manual: s.manual } })
  return true
}

export function useWorld<T>(selector: (s: WorldState) => T): T {
  return useStore(worldStore, selector)
}

/** HUD wording of limitedBy (M05 §8.2); the UI keeps its own i18n keys, this is the reference mapping */
export const LIMITED_BY_KEYS: Readonly<Record<LimitedByName, string>> = {
  budget: 'pc.limitedBy.budget', nodes: 'pc.limitedBy.nodes', headroom: 'pc.limitedBy.headroom', error: 'pc.limitedBy.error', complete: 'pc.limitedBy.complete',
}
