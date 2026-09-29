// Telemetry icon buckets with hysteresis and dwell (M15-FR-065; d03 §3.6): battery edges 20/40/70%, a higher level needs
// the edge + 3%, a lower level applies at once; the same icon slot keeps a level >= 1.5 s (alarm escalation is not
// delayed), so telemetry-driven icon changes stay <= 0.7 Hz.
import { useState } from 'react'
import { ICON_BUCKETS, INPUT } from '@/lib/tokens/input.gen'
import type { IconKey } from './registry'

export const BATTERY_KEYS: readonly IconKey[] = ['bat.warn', 'bat.low', 'bat.medium', 'bat.full']
export const LINK_KEYS: readonly IconKey[] = ['link.lost', 'link.low', 'link.medium', 'link.high']

/** next level for a value in percent: up needs edge + hysteresis, down is immediate */
export function nextLevel(pct: number, prev: number, edges: readonly number[] = ICON_BUCKETS.batteryEdgesPct, hyst: number = ICON_BUCKETS.hysteresisPct): number {
  let l = prev
  while (l < edges.length && pct >= edges[l] + hyst) l++
  while (l > 0 && pct < edges[l - 1]) l--
  return l
}

export interface BucketState { level: number; since: number }
/** pure step used by the hook and the tests: dwell INPUT.iconDwellMs unless it is an alarm escalation */
export function bucketStep(st: BucketState, pct: number, nowMs: number, alarm: boolean, edges: readonly number[]): BucketState {
  const target = nextLevel(pct, st.level, edges)
  if (target === st.level) return st
  const escalation = alarm && target < st.level
  if (!escalation && nowMs - st.since < INPUT.iconDwellMs) return st
  return { level: target, since: nowMs }
}

/**
 * Bucketed battery or link icon key. `sampleMs` is the time of the telemetry sample on the performance.now() clock
 * (the dwell is measured between samples, so rendering stays pure); the level follows the samples while rendering.
 */
export function useBucketedIcon(kind: 'battery' | 'link', value: number, sampleMs: number, alarm = false): IconKey {
  const edges = kind === 'battery' ? ICON_BUCKETS.batteryEdgesPct : ICON_BUCKETS.linkEdgesPct
  const keys = kind === 'battery' ? BATTERY_KEYS : LINK_KEYS
  const [st, setSt] = useState<BucketState>(() => ({ level: edges.length, since: Number.NEGATIVE_INFINITY }))
  const next = Number.isFinite(value) ? bucketStep(st, value, sampleMs, alarm, edges) : st
  if (next !== st) setSt(next)
  return keys[next.level]
}
