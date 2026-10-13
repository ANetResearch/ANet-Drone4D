// Power-save view store (ADR-121; M15-FR-227, FR-228). Owner: M15. Written only by app/power/PowerController.ts and
// app/power/viewerQueue.ts; read by the countdown bar, the overlay, the queue card, the frozen texts and RtProvider
// (`linkOpen`). Kept apart from the controller so the queue client and the controller do not import each other.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { POWER_DEFAULTS, type PowerRole, type PowerState, type PowerThresholds, type SaveReason } from './machine'

export interface ViewerTicket {
  ticketId: string
  ticketToken: string
  position: number
  etaS: number | null
  state: 'waiting' | 'seat_ready' | 'expired'
}

export interface ResumeProgress {
  kind: 'viewer' | 'sandbox'
  /** segment fractions (viewer: reconnect 0.3, state 0.5, reveal 0.2; sandbox: spawn 0.5, state 0.3, reveal 0.2) */
  seg: Record<string, number>
  queued: boolean
}
export interface PowerView {
  state: PowerState
  role: PowerRole
  reason: SaveReason | null
  /** countdown of the warning bar (s) */
  warnLeftS: number
  /** the realtime link may be open (false while saving or queued for a seat): RtProvider follows it */
  linkOpen: boolean
  /** wall ms of the last live update before saving (the "last update" texts) */
  lastUpdateMs: number
  /** simulation time (ns) shown when saving began (the sandbox overlay's "sim time" until the server's t_sim_ns arrives) */
  lastSimNs: number | null
  /** texts frozen (HUD, session bar, timeline): saving, resuming or queued */
  frozen: boolean
  /** the frame-interval figures are not judged (no red, "paused"): frozen, and for PERF_SETTLE_MS after the page is live
   *  again, while the 2 s summary window still holds the suspended gap (d2-WP-13b-to-M15 item 5) */
  perfPaused: boolean
  /** the viewer queue ticket (seats full) */
  viewerTicket: ViewerTicket | null
  resume: ResumeProgress | null
  /** internal identity: "keep running" (no countdown, no saving; the server's fallback is 4 h) */
  keepRunning: boolean
  thresholds: PowerThresholds
  version: number
}

export const powerStore = createAwrStore<PowerView>('ui.power', () => ({
  state: 'ACTIVE', role: 'none', reason: null, warnLeftS: 0, linkOpen: true, lastUpdateMs: Date.now(), lastSimNs: null, frozen: false, perfPaused: false, viewerTicket: null, resume: null,
  keepRunning: false, thresholds: { ...POWER_DEFAULTS }, version: 0,
}))
export const usePower = <T,>(sel: (s: PowerView) => T): T => useStore(powerStore, sel)

/** the perf summary window (stores/perf: last 2 s of frame intervals) plus one UI tick */
export const PERF_SETTLE_MS = 2500
