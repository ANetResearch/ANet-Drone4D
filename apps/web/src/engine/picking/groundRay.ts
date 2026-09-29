// Ground and building picking through M04 ray_hit (M06 §6.12, FR-062, AC-044; AWR-17 §4.3.2; AWR-10 AD-04). Owner: M06.
// POST /api/world/{id}/query {op: "ray_hit", origin_enu_m, dir, max_range_m: 5000}; the previous request is aborted
// before a new one (AbortController), each request times out after 1 s; previews are throttled to input.rayPreviewHz
// (5 Hz), clicks are sent at once. Failures and timeouts are M06-E015 (the UI hides the preview).
import { INPUT } from '@/lib/tokens/input.gen'
import { worldQuery, type RayHitRequest, type RayHitResponse } from '@/net/api'

export const GROUND_PICK = { maxRangeM: 5000, timeoutMs: 1000 } as const

export type GroundResult =
  | { kind: 'ground'; pointEnu: Float64Array; surface: string; distM: number }
  | { kind: 'none'; reason: 'miss' | 'timeout' | 'error' | 'aborted' | 'throttled'; message?: string }

export type QueryFn = (worldId: string, req: RayHitRequest, signal: AbortSignal) => Promise<RayHitResponse>

export class GroundRay {
  private inflight: AbortController | null = null
  private lastPreviewMs = Number.NEGATIVE_INFINITY
  requests = 0
  aborted = 0
  failures = 0

  constructor(private readonly query: QueryFn = (w, r, s) => worldQuery<RayHitResponse>(w, r, s), private readonly now: () => number = () => performance.now()) {}

  /** preview: at most rayPreviewHz; returns 'throttled' without a request inside the interval */
  preview(worldId: string, o: ArrayLike<number>, d: ArrayLike<number>): Promise<GroundResult> {
    const t = this.now()
    if (t - this.lastPreviewMs < 1000 / INPUT.rayPreviewHz) return Promise.resolve({ kind: 'none', reason: 'throttled' })
    this.lastPreviewMs = t
    return this.hit(worldId, o, d)
  }

  /** a click: sent immediately (aborts any preview in flight) */
  async hit(worldId: string, o: ArrayLike<number>, d: ArrayLike<number>, signal?: AbortSignal): Promise<GroundResult> {
    if (this.inflight) {
      this.inflight.abort()
      this.aborted++
    }
    const ac = new AbortController()
    this.inflight = ac
    const onOuter = (): void => ac.abort()
    signal?.addEventListener('abort', onOuter)
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      ac.abort()
    }, GROUND_PICK.timeoutMs)
    this.requests++
    const req: RayHitRequest = { op: 'ray_hit', origin_enu_m: [o[0], o[1], o[2]], dir: [d[0], d[1], d[2]], max_range_m: GROUND_PICK.maxRangeM }
    try {
      const r = await this.query(worldId, req, ac.signal)
      if (!r.hit || !r.point_enu_m) return { kind: 'none', reason: 'miss' }
      return { kind: 'ground', pointEnu: Float64Array.from(r.point_enu_m), surface: r.surface, distM: r.dist_m ?? Number.NaN }
    } catch (e) {
      if (!ac.signal.aborted) this.failures++
      if (timedOut) console.warn('M06-E015 ray_hit timed out')
      else if (!ac.signal.aborted) console.warn(`M06-E015 ray_hit failed: ${String((e as Error)?.message ?? e)}`)
      return { kind: 'none', reason: timedOut ? 'timeout' : ac.signal.aborted ? 'aborted' : 'error', message: String((e as Error)?.message ?? e) }
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onOuter)
      if (this.inflight === ac) this.inflight = null
    }
  }

  cancel(): void {
    this.inflight?.abort()
    this.inflight = null
  }
}
