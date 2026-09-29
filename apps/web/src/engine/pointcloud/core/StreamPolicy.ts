// Fetch cancellation and retry policy (M05 §6.5.2, M05-FR-017, FR-018; port of voxelkloud-view stream-policy.ts with
// the constants kept and the back-off moved to wall-clock time). Owner: M05. Pure: no DOM, no clock, no randomness.
import { PC } from '../params'

/** frames out of the selection and outside the frustum before an in-flight fetch is aborted */
export const ABORT_OUTSIDE_FRAMES = PC.abortOutsideFrames
/** frames out of the selection before a superseded fetch is aborted (off by default) */
export const ABORT_STALE_FRAMES = PC.abortStaleFrames
export const MAX_LOAD_ATTEMPTS = PC.maxAttempts

export interface AbortPolicy {
  abortOutside: boolean
  abortSuperseded: boolean
  /** all fetch slots taken (gates the superseded tier only) */
  saturated: boolean
}

/**
 * Whether to cancel one in-flight fetch.
 * @param staleFrames frames since the node was last selected (0 = selected this frame, never cancels)
 * @param outsideFrustum the node's tight box is fully outside the frustum this frame
 */
export function shouldAbort(staleFrames: number, outsideFrustum: boolean, p: AbortPolicy): boolean {
  if (staleFrames <= 0) return false
  if (p.abortOutside && outsideFrustum && staleFrames >= ABORT_OUTSIDE_FRAMES) return true
  return p.abortSuperseded && p.saturated && staleFrames >= ABORT_STALE_FRAMES
}

/** back-off after the n-th failure: 0.5, 2, 8 s (60 Hz: 30, 120, 480 frames) */
export const retryDelayMs = (attempts: number): number => PC.retryBaseMs * PC.retryFactor ** Math.max(0, attempts - 1)

/**
 * Wall-clock time of the next allowed attempt after `attempts` failures (attempts counted after the failure):
 * 1 -> now + 0.5 s, 2 -> now + 2 s, >= 3 -> FAILED and re-queued at now + 10 s (M05 §6.5.2; C46).
 */
export function nextRetryAt(attempts: number, nowMs: number): number {
  if (attempts >= MAX_LOAD_ATTEMPTS) return nowMs + PC.failedRequeueMs
  return nowMs + retryDelayMs(attempts)
}

/** true when the node is marked FAILED after this many attempts */
export const isFailedAfter = (attempts: number): boolean => attempts >= MAX_LOAD_ATTEMPTS

/** fail kinds of the worker protocol (M05 §6.5.1): http and network retry, length fails at once, abort never counts */
export type FailKind = 'http' | 'network' | 'length' | 'abort'
export function failCounts(kind: FailKind): boolean {
  return kind !== 'abort'
}
export function failIsFinal(kind: FailKind): boolean {
  return kind === 'length'
}
