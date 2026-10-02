// Shared UI refresh tick (ADR-066; AWR-18 §6.1 item 6; M15-NFR-001, D1-AC-23). Every periodic UI publisher (the fleet,
// timeline, perf and sensors summaries, C-class text, chart redraws on Tier S) writes in the same frame, at the telemetry
// text cadence of the tier (--telemetry-text-interval: Tier S 250 ms, Tier B/A 100 ms). With one 4 Hz phase per publisher
// the DOM and canvas changes of the shell landed in nearly every Tier S frame, and on SwiftShader a frame that carries
// raster work or texture uploads costs the GPU process far more than a pure composite (ACC-1 §4.2; FX2-R2-web-ui report).
// The first caller in a frame decides whether the frame is a tick frame; publishers keep their own loop registrations
// (ids, tiers and perf layers stay) and gate on uiTickDue(ctx) instead of their own fps option.
import { loop, type FrameCtx } from '@/engine/loop'
import { MOTION } from '@/lib/tokens/motion.gen'

/** frame number and time of the last tick (-1 and -Infinity before the first one) */
export const uiTick = { frameNo: -1, atMs: Number.NEGATIVE_INFINITY, count: 0 }

/** period of the tick on a tier (ms): the C-class text interval token */
export const uiTickPeriodMs = (tier: FrameCtx['tier']): number => MOTION.telemetryTextIntervalMs[tier]

/** true in a tick frame; the first call of a frame decides (frame boundaries quantise the period like loop fps tasks) */
export function uiTickDue(ctx: FrameCtx): boolean {
  if (ctx.frameNo === uiTick.frameNo) return true
  if (ctx.nowMs >= uiTick.atMs && ctx.nowMs - uiTick.atMs < uiTickPeriodMs(ctx.tier) - 1) return false
  uiTick.frameNo = ctx.frameNo
  uiTick.atMs = ctx.nowMs
  uiTick.count++
  return true
}

/** test helper: forget the last tick */
export function resetUiTick(): void {
  uiTick.frameNo = -1
  uiTick.atMs = Number.NEGATIVE_INFINITY
  uiTick.count = 0
}

// decided at the start of the overlay phase, so that every later publisher of the frame (overlay and governor) agrees
loop.register('overlay', 'ui.tick', (ctx) => void uiTickDue(ctx), { order: -1000 })
