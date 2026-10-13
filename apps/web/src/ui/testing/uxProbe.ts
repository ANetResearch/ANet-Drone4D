// window.__ux test probe (M15-FR-010, §7.1.7): dev and test builds only (TEST_SWITCHES is a build-time constant, so the
// probe is dropped from plain production builds, M15-AC-002). The object is preallocated and updated in place.
// Production-build performance specs must not read it (AWR-18 PR-7): they use DOM queries and window.__perf.
import { TEST_SWITCHES } from '@/lib/testSwitches'

export interface UxProbe {
  /** D2 (AWR-14 §20.3.3 rule 6): every bar write (value, first incomplete segment), ring of 256 */
  boot: { state: string; revealAt: number; progress: { tMs: number; p: number; seg: string | null }[]; capacity: string }
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
  /** D2 sandbox surface (AWR-14 §20.22): surface, session and queue phases, setpoints sent by the manual pump */
  sandbox: { surface: string; phase: string; queue: string; setpoints: number; keepalives: number }
  /** services of the page client's calls, ring of 256 (M15-AC-214: a viewer issues no write call) */
  rtCalls: string[]
  /** D2.1 (AWR-04 §15.7, §15.8, §15.9): power-save state and transitions, presence messages sent, form factor, identity tier,
   *  quick-command steps and results */
  power: { state: string; transitions: string[]; presence: number; reason: string; frames: number; conn: string; link: boolean }
  ff: { ff: string; coarse: boolean; orient: string; changes: number }
  identity: { tier: string }
  quick: { state: string; sent: string[] }
  /** manual control (D2-AC-21, D2-AC-48; D2-FX-R2-web): state, pump ticks, ticks that sent nothing and why, the stick
   *  axes last written, the clamp state and the predicted entry */
  manual: { state: string; ticks: number; idle: number; noAgent: number; stick: number[]; clamped: boolean; approachS: number | null; ended: string }
}

export const UX: UxProbe = {
  boot: { state: 'SHELL', revealAt: Number.NaN, progress: [], capacity: 'idle' },
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
  sandbox: { surface: 'local', phase: 'none', queue: 'none', setpoints: 0, keepalives: 0 },
  rtCalls: [],
  power: { state: 'ACTIVE', transitions: [], presence: 0, reason: '', frames: 0, conn: 'IDLE', link: true },
  ff: { ff: 'desktop', coarse: false, orient: 'landscape', changes: 0 },
  identity: { tier: 'public' },
  quick: { state: 'CLOSED', sent: [] },
  manual: { state: 'IDLE', ticks: 0, idle: 0, noAgent: 0, stick: [0, 0, 0, 0], clamped: false, approachS: null, ended: '' },
}

export function installUx(): void {
  if (TEST_SWITCHES && typeof window !== 'undefined') (window as unknown as { __ux: UxProbe }).__ux = UX
}

/** test builds: record the service of every call of the page client in UX.rtCalls (the client is returned unchanged otherwise) */
export function probeRtCalls<C extends { call: (service: string, args: object, o?: never) => unknown }>(client: C): C {
  if (!TEST_SWITCHES) return client
  const call = client.call.bind(client) as (service: string, args: object, o?: unknown) => unknown
  ;(client as { call: (service: string, args: object, o?: unknown) => unknown }).call = (service, args, o) => {
    UX.rtCalls.push(service)
    if (UX.rtCalls.length > 256) UX.rtCalls.shift()
    return call(service, args, o)
  }
  return client
}

export function uxDefect(name: string): void {
  if (TEST_SWITCHES) UX.defects[name] = (UX.defects[name] ?? 0) + 1
}
