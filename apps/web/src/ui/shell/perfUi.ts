// window.__perf.ui (M15-FR-096; AWR-18 §9.2): preallocated and updated in place by the store factory, LfScheduler,
// StateIcon and the motion tier resolver. M15 writes only the `ui` subtree; M06 owns window.__perf and must keep an
// existing `ui` object when it creates the probe (request .cache/impl/requests/M15-to-M06.md).
export interface PerfUi {
  storeWrites: Record<string, number>
  charts: { drawsMax: number; drawMsMax: number; hiddenDraws: number; streamingVisible: number; svgHzMax: number }
  icons: { active: number; activeMax: number; denied: number; minSwitchIntervalMs: number }
  motionTier: 'full' | 'lite' | 'reduced'
}

export const PERF_UI: PerfUi = {
  storeWrites: {},
  charts: { drawsMax: 0, drawMsMax: 0, hiddenDraws: 0, streamingVisible: 0, svgHzMax: 0 },
  icons: { active: 0, activeMax: 0, denied: 0, minSwitchIntervalMs: Number.POSITIVE_INFINITY },
  motionTier: 'lite',
}

type PerfHost = { __perf?: { ui?: PerfUi } & Record<string, unknown> }

/** Attach PERF_UI to window.__perf (creating the holder when M06 has not created the probe yet). Idempotent. */
export function installPerfUi(): PerfUi {
  if (typeof window === 'undefined') return PERF_UI
  const w = window as unknown as PerfHost
  w.__perf ??= {}
  w.__perf.ui = PERF_UI
  return PERF_UI
}
