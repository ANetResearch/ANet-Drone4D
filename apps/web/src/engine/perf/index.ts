// engine/perf facade (M06 §6.18, §7.1; AWR-18 §9). Owner: M06.
// perfProbe() creates window.__perf once (keeping the `ui` subtree M15 created earlier, M15-to-M06 item 1), installs the
// FrameSampler hooks on the loop, the LoAF observer and, in test builds, the busy-wait injection tasks
// (?perfInject=busyMs:n in the overlay phase, renderBusyMs:n in the render phase; AWR-18 §4.7, §6.3). perf.markReveal()
// is called once by the M15 BootController when the mask is revealed (load.revealAt, load.tti, programs baseline,
// 30 frozen frames, ADR-012). The PerfGovernor and the latency meter are page singletons.
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { ctx, loop, register } from '../loop'
import { makeProbe, busyWait, INJECT, type AwrPerf } from './probe'
import { frameEnd, frameStart, sampler } from './frameSampler'
import { observeLoaf } from './loaf'
import { PerfGovernor, type GovernorKnob } from './governor'
import { LatencyMeter } from './latency'

export { ring, pushRing, tailRing, lastOf, RING_CAP, LAYER_BUDGET_MS, INJECT, busyWait, pushGovernorHistory, pushLoafWorst, type Ring, type AwrPerf, type ForcedFlags, type LoafWorst, type GovernorEntry, type UboProbe } from './probe'
export { refreshMs, onRefreshChange, setSoftware, sampler as frameSampler, feedInterval } from './frameSampler'
export { isOurScript, isLoopCallback, oursOutsideLoop } from './loaf'
export { PerfGovernor, GOVERNOR, type GovernorKnob, type GovernorStepNo, type CasHandle, type CasState } from './governor'
export { LatencyMeter, LATENCY, type CmdKind } from './latency'

type PerfHost = { __perf?: Record<string, unknown> }
let probe: AwrPerf | null = null
let gov: PerfGovernor | null = null
let lat: LatencyMeter | null = null

/** create (once) and return window.__perf (in Node tests a detached probe) */
export function perfProbe(): AwrPerf {
  if (probe) return probe
  probe = makeProbe(TEST_SWITCHES) as AwrPerf
  probe.meta.mode = TEST_SWITCHES ? 'test' : 'production'
  if (typeof window !== 'undefined') {
    const w = window as unknown as PerfHost
    const prev = w.__perf
    if (prev) {
      if (prev.ui !== undefined) probe.ui = prev.ui
      const pl = prev.load as Record<string, number> | undefined
      if (pl?.revealAt) probe.load.revealAt = pl.revealAt
      if (pl?.tti) probe.load.tti = pl.tti
    }
    probe.meta.crossOriginIsolated = typeof crossOriginIsolated !== 'undefined' && crossOriginIsolated
    w.__perf = probe as unknown as Record<string, unknown>
    const p = probe
    loop.setFrameHook((now) => frameStart(p, now))
    loop.setFrameEndHook(() => frameEnd(p))
    observeLoaf(p)
    if (TEST_SWITCHES) {
      register('overlay', 'perf.inject', () => busyWait(INJECT.busyMs), { order: 10_000 })
      register('render', 'perf.inject.render', () => busyWait(INJECT.renderBusyMs), { order: -10_000 })
    }
  }
  return probe
}

/** the page PerfGovernor (M06-FR-075); knobs from M05 (step 7) and M07 (step 5) via perf.registerKnob */
export function governor(): PerfGovernor {
  if (!gov) gov = new PerfGovernor({ tier: () => ctx.tier, lowestAllowedRung: () => ctx.be?.lowestAllowedRung ?? 2, perf: perfProbe() })
  return gov
}

export function latency(): LatencyMeter {
  if (!lat) lat = new LatencyMeter(perfProbe())
  return lat
}

let revealed = false
const revealListeners = new Set<() => void>()
export const REVEAL_FREEZE_FRAMES = 30
export const perf = {
  /** writes __perf.load.revealAt and load.tti once (ms, performance.now() base); later calls are ignored */
  markReveal(): void {
    if (revealed || typeof window === 'undefined') return
    revealed = true
    const now = performance.now()
    const p = perfProbe()
    p.load.revealAt = now
    p.load.tti = now
    p.gpu.programsAtReveal = p.gpu.programs
    sampler.revealed = true
    loop.freeze(REVEAL_FREEZE_FRAMES)
    for (const cb of revealListeners) cb()
  },
  get revealed(): boolean {
    return revealed
  },
  onReveal(cb: () => void): () => void {
    if (revealed) cb()
    revealListeners.add(cb)
    return () => {
      revealListeners.delete(cb)
    }
  },
  probe: perfProbe,
  /** PerfGovernor knob registration (M06 §7.1 governor.registerKnob) */
  registerKnob(k: GovernorKnob): () => void {
    return governor().registerKnob(k)
  },
}
