// Time runtime wiring (M12 §6.3, §6.4, §7.1, FR-006, FR-007, FR-011; AWR-03 §3.6). Owner: M12.
// initTime() creates the page's SimClockView + InterpRing pair. ingest(frame) is called by M06's telemetry task right
// after RtClient.swapFrame(): TIME from the slot header (with its worker receive time), the clock offset, epoch changes
// (ring cleared before the new epoch's samples, onEpoch listeners), RESET channels (only that producer's agent range),
// then the swarm rows and Full64 items into the ring, the swarm arrival for D_wall and focus arrivals for D_focus.
// The clock phase task ('clock', 'm12.clock') ticks the clock, writes FrameCtx.tRenderS/tFocusS/simRate/clockState,
// sets the extrapolation limit E_max = rate × 3 / hz_eff while advancing (held while frozen; after a frozen epoch change
// at least one recording block interval so a seek target can be reached), and updates window.__perf.time in place.
// Test builds (M07-NFR-019, M07-AC-021): ?simTime=<s>&paused=1 pins tRender and tFocus at <s> (clock state PAUSED, rate 0)
// so environment and interpolation render a fixed simulation instant and RT read-backs are comparable; window.__time
// .setFixed(s) moves the pinned instant (forward playback in steps) and .setFixed(null) releases it (FX-WEB1).
// No allocation on the frame path.
import { SF } from '@/net/rt/frame'
import { rtClient } from '@/net/rt/client'
import type { RosterView, TelemetryFrame } from '@/net/rt/types'
import type { FrameCtx, register as registerFn } from '../loop'
import { SimClockView } from './clockView'
import { epochBus, type ResetRange } from './epochBus'
import { InterpRing, type DronePoseSoA } from './interpRing'
import { TIME_PARAMS as P, TS } from './params'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { CostWindow, RatioWindow, installPerfTime, perfTime } from './perfTime'

export interface TimeRuntime {
  readonly clock: SimClockView
  readonly interp: InterpRing
  /** main-thread time base: server monotonic ms = now + clockOffsetMainMs */
  readonly offset: { mainMs: number }
  ingest(frame: TelemetryFrame, nowMs: number): void
  sampleSwarm(tS: number, out: DronePoseSoA): void
  sampleOne(agentNo: number, tS: number, out: DronePoseSoA, o?: number): boolean
  setFocus(agentNo: number): void
  /** recording block interval (ms) used as the extrapolation floor after a frozen epoch change (replay decimation) */
  setBlockIntervalMs(ms: number | null): void
  /** called once per epoch with the frame time of the first samples of that epoch (seek latency) */
  onEpochData: ((epoch: number, nowMs: number) => void) | null
  dispose(): void
}

export interface TimeDeps {
  register: typeof registerFn
  /** roster used to map RESET channels to a producer range (defaults to the page RtClient's) */
  roster?: () => RosterView | null
  /** reduced motion (defaults to <html data-motion="reduced|off">) */
  reducedMotion?: () => boolean
}

/** test builds only: pinned render time (seconds) of ?simTime=<s>&paused=1 (M07-NFR-019) */
export const fixedTime = { on: false, tS: 0 }
if (TEST_SWITCHES && typeof location !== 'undefined') {
  const q = new URLSearchParams(location.search)
  const t = Number(q.get('simTime'))
  if (q.get('paused') === '1' && q.has('simTime') && Number.isFinite(t)) {
    fixedTime.on = true
    fixedTime.tS = t
  }
}
if (TEST_SWITCHES && typeof window !== 'undefined') {
  ;(window as unknown as { __time: unknown }).__time = {
    setFixed(t: number | null): void {
      fixedTime.on = t !== null && Number.isFinite(t)
      if (fixedTime.on) fixedTime.tS = t as number
    },
    fixed: () => (fixedTime.on ? fixedTime.tS : null),
  }
}

let current: TimeRuntime | null = null
/** the page's time runtime (created by the drones runtime of M06); null before the viewport starts */
export function timeRuntime(): TimeRuntime | null {
  return current
}

const FULL_ITEM = 80
const docReduced = (): boolean => {
  if (typeof document === 'undefined') return false
  const m = document.documentElement?.dataset?.motion
  return m === 'reduced' || m === 'off'
}

export function initTime(deps: TimeDeps): TimeRuntime {
  const clock = new SimClockView()
  const interp = new InterpRing()
  const offset = { mainMs: Number.NaN }
  const rosterOf = deps.roster ?? (() => rtClient()?.roster ?? null)
  const reducedOf = deps.reducedMotion ?? docReduced
  const clockCost = new CostWindow()
  const sampleCost = new CostWindow()
  const ratios = new RatioWindow()
  let clearedEpoch = -1
  let dataEpoch = -1
  let frozenEpochFloor = false
  let blockMs: number = P.blockIntervalMs
  let lastP95At = Number.NEGATIVE_INFINITY
  let frame = 0
  let installed = false

  interp.onFocusChange = (a) => clock.delay.setFocus(a >= 0)

  const clearForEpoch = (e: number): void => {
    if (e === clearedEpoch) return
    clearedEpoch = e
    interp.clearAll()
    if (!clock.advancing) frozenEpochFloor = true
  }

  /** producer range of a RESET channel: the agent of a Full64 item on that channel, else sim-core; roster min..max */
  const resetRange = (f: TelemetryFrame, channelId: number, out: ResetRange): boolean => {
    const roster = rosterOf()
    let producer = 'sim-core'
    const fr = f.full
    for (let k = 0; k < fr.count; k++) {
      const o = fr.base + FULL_ITEM * k
      if (fr.bytes.getUint16(o + 2, true) === channelId) {
        producer = roster?.get(fr.bytes.getUint16(o, true))?.producer ?? producer
        break
      }
    }
    if (!roster || roster.size === 0) {
      out.idBase = 0
      out.idCount = 65536
      return true
    }
    let lo = 65536
    let hi = -1
    for (const e of roster.entries()) {
      if (e.producer !== producer) continue
      if (e.agentNo < lo) lo = e.agentNo
      if (e.agentNo > hi) hi = e.agentNo
    }
    if (hi < lo) return false
    out.idBase = lo
    out.idCount = hi - lo + 1
    return true
  }
  const range: ResetRange = { idBase: 0, idCount: 0 }

  const ingest = (f: TelemetryFrame, nowMs: number): void => {
    const t0 = typeof performance !== 'undefined' ? performance.now() : 0
    const h = f.hdr
    if (Number.isFinite(h.clockOffsetMainMs)) offset.mainMs = h.clockOffsetMainMs
    const te = h.timeEpoch & 0xffff
    if ((h.flags & SF.TIME_CHANGED) !== 0 || (clock.hasTime && (te !== clock.epoch || h.timeTSimMs !== clock.tSimMs || (h.timeState & 0x0f) !== clock.state4))) {
      const prevEpoch = clock.epoch
      const recv = h.timeRecvMainMs > 0 ? h.timeRecvMainMs : nowMs
      clock.onTimeFields(h.timeState, te, h.timeRate, h.timeTSimMs, h.timeTSrvMs, recv)
      if (clock.epoch !== prevEpoch) clearForEpoch(clock.epoch)
    }
    if ((h.flags & SF.EPOCH_CHANGED) !== 0) clearForEpoch(h.epoch & 0xffff)
    for (let i = 0; i < h.resetCount; i++) {
      const ch = f.resetChannelIds[i]
      let dup = false
      for (let j = 0; j < i; j++) if (f.resetChannelIds[j] === ch) dup = true
      if (dup || !resetRange(f, ch, range)) continue
      interp.clearRange(range.idBase, range.idCount)
      epochBus.emitReset(range)
    }
    const focusNew = interp.ingest(f)
    if (h.swarmN > 0) {
      clock.delay.onSample(h.swarmRecvMainMs > 0 ? h.swarmRecvMainMs : nowMs)
      if (dataEpoch !== clock.epoch) {
        dataEpoch = clock.epoch
        rt.onEpochData?.(dataEpoch, nowMs)
      }
    }
    if (focusNew) clock.delay.onFocusSample(nowMs, h.selHz, h.selJitterMs)
    if (typeof performance !== 'undefined') perfTime.ingestMs = performance.now() - t0
  }

  const off = deps.register('clock', 'm12.clock', (ctx: FrameCtx) => {
    const t0 = typeof performance !== 'undefined' ? performance.now() : 0
    if ((frame++ & 31) === 0) {
      clock.reduced = reducedOf()
      if (!installed) {
        installPerfTime()
        installed = typeof window === 'undefined' || (window as unknown as { __perf?: { time?: unknown } }).__perf?.time === perfTime
      }
    }
    clock.delay.zeroWhenFrozen = perfTime.frozenZeroD
    clock.tick(ctx.nowMs, ctx.nowMs + offset.mainMs)
    ctx.tRenderS = clock.tRenderS()
    ctx.tFocusS = clock.tFocusS()
    ctx.simRate = clock.rate
    ctx.clockState = clock.state4
    if (TEST_SWITCHES && fixedTime.on) {
      ctx.tRenderS = fixedTime.tS
      ctx.tFocusS = fixedTime.tS
      ctx.simRate = 0
      ctx.clockState = TS.PAUSED
    }
    const d = clock.delay
    const hz = d.hzEff
    if (clock.advancing) {
      frozenEpochFloor = false
      if (hz > 0) interp.eMaxMs = (clock.rate * P.extrapIntervals * 1000) / hz
    } else if (frozenEpochFloor) {
      const live = hz > 0 ? (clock.rateNominal * 1000) / hz : blockMs
      interp.eMaxMs = Math.max(interp.eMaxMs, clock.replay ? blockMs : live)
      frozenEpochFloor = false
    }
    interp.rate = clock.rateNominal
    // __perf.time (M12 §10.3)
    const pt = perfTime
    pt.simNowS = clock.simNowS()
    pt.tRenderS = ctx.tRenderS
    pt.tFocusS = ctx.tFocusS
    pt.dGlobalMs = d.dSimMs
    pt.dFocusMs = d.dFocusSimMs
    pt.dWallMs = d.dWallMs
    pt.hzEff = hz
    pt.jitterP95Ms = d.jitterMs
    pt.rate = clock.rate
    pt.state4 = clock.state4
    pt.epoch = clock.epoch
    pt.stale = clock.stale
    pt.replay = clock.replay
    pt.focusLowLatency = d.focusLowLatency
    pt.focusSettled = d.focusSettled
    pt.clockSnaps = clock.clockSnaps
    pt.frames++
    ratios.roll(ctx.nowMs, pt)
    if (typeof performance !== 'undefined') {
      pt.clockMs = performance.now() - t0
      clockCost.push(pt.clockMs)
      if (ctx.nowMs - lastP95At >= P.perfWindowMs) {
        lastP95At = ctx.nowMs
        pt.clockP95Ms = clockCost.p95()
        pt.sampleP95Ms = sampleCost.p95()
      }
    }
  })

  const rt: TimeRuntime = {
    clock, interp, offset, ingest,
    sampleSwarm(tS, out) {
      const t0 = typeof performance !== 'undefined' ? performance.now() : 0
      interp.sampleSwarm(tS, out)
      if (typeof performance !== 'undefined') {
        perfTime.sampleMs = performance.now() - t0
        sampleCost.push(perfTime.sampleMs)
      }
      ratios.add(interp.lastN, interp.lastHold, interp.lastExtrap, interp.lastMaxAgeMs)
    },
    sampleOne: (agentNo, tS, out, o = 0) => interp.sampleOne(agentNo, tS, out, o),
    setFocus: (a) => interp.setFocus(a),
    setBlockIntervalMs(ms) {
      blockMs = ms !== null && ms > 0 ? ms : P.blockIntervalMs
    },
    onEpochData: null,
    dispose() {
      off()
      if (current === rt) current = null
    },
  }
  current = rt
  return rt
}
