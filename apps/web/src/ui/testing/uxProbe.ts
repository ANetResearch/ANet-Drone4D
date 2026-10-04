// window.__ux test probe (M15-FR-010, §7.1.7): dev and test builds only (TEST_SWITCHES is a build-time constant, so the
// probe is dropped from plain production builds, M15-AC-002). The object is preallocated and updated in place.
// Production-build performance specs must not read it (AWR-18 PR-7): they use DOM queries and window.__perf.
import { TEST_SWITCHES } from '@/lib/testSwitches'

export interface UxProbe {
  boot: { state: string; revealAt: number }
  camera: { mode: string; followLock: boolean; lastFlight: { durationMs: number; d_m: number } }
  selection: { ids: string[]; primary: string | null }
  toasts: { visible: number; merged: Record<string, number>; headless: number }
  red: Record<string, { kind: string; id: string } | null>
  rayHitRequests: number
  droneRail: { renderedRows: number; visibleRows: number }
  motion: { tier: string; source: string }
  budget: { blurMax: number; popPerSecMax: number; loopsMax: number }
  layout: { unobscured: { x: number; y: number; w: number; h: number }; breakpoint: string; unobscuredCommits: number }
  bridge: { flushes: number; over2ms: number; maxMs: number; dropped: number }
  defects: Record<string, number>
}

export const UX: UxProbe = {
  boot: { state: 'SHELL', revealAt: Number.NaN },
  camera: { mode: 'orbit', followLock: false, lastFlight: { durationMs: 0, d_m: 0 } },
  selection: { ids: [], primary: null },
  toasts: { visible: 0, merged: {}, headless: 0 },
  red: {},
  rayHitRequests: 0,
  droneRail: { renderedRows: 0, visibleRows: 0 },
  motion: { tier: 'lite', source: 'governor' },
  budget: { blurMax: 0, popPerSecMax: 0, loopsMax: 0 },
  layout: { unobscured: { x: 0, y: 0, w: 0, h: 0 }, breakpoint: 'S', unobscuredCommits: 0 },
  bridge: { flushes: 0, over2ms: 0, maxMs: 0, dropped: 0 },
  defects: {},
}

export function installUx(): void {
  if (TEST_SWITCHES && typeof window !== 'undefined') (window as unknown as { __ux: UxProbe }).__ux = UX
}

export function uxDefect(name: string): void {
  if (TEST_SWITCHES) UX.defects[name] = (UX.defects[name] ?? 0) + 1
}
