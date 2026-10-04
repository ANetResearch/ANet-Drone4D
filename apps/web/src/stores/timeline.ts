// Timeline store (M12 §7.1 fields and actions, FR-014 to FR-028, FR-020; AWR-14 §6.17). Owner: M12.
// Vanilla zustand store through createAwrStore; M15 renders it (TimelineBar, TimelinePanel, LfTimelineTrack over
// `timelineTrack`). High-rate values (tRender, D) never enter the store: one overlay-phase task (Tier S 4 Hz, B/A 10 Hz)
// reads the engine/time facade and writes at most one merged update per run (tDisplayS, range, view, TIME summary, RTF
// badge, pending timeout, data versions), so the store writes <= 4 times per second on Tier S (M12-AC-020). User actions
// write once when they start (pending ring) and once when confirmed.
// Transport: live controls call sim/play, sim/pause, sim/step {ticks}, sim/speed {rate}; replay controls send playback
// {play, pause, seek, speed}. A control stays pending until TIME reaches its target, the call is rejected (reason text),
// or 1 s passes (revert, "no confirmation"). Replay seeks move the playhead optimistically, show BUFFERING after 100 ms
// without playbackState{did_seek}, and measure the first backfill frame (lastSeekMs). Markers come from the `event`
// channel live (plus a GET /api/events backfill after (re)connection) and from the segment .evx in replay; bookmarks are
// shared through runs REST (operator, admin) or kept per run in localStorage (viewer).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { sanitizeText } from '@/lib/sanitize'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { INPUT } from '@/lib/tokens/input.gen'
import { loop } from '@/engine/loop'
import {
  PendingMachine, RtfWatch, TIME_PARAMS, TIME_STATE as TS, TrackModel, guards, liveStepMs, LIVE_STEP_TICKS, markerClassOf,
  newDronePoseSoA, parseEvx, parseOvw, ovwBinMs, perfTime, reasonKey, replayStepTarget, timeRuntime,
  type Control, type Guards, type StepKind, type TrackRange,
} from '@/engine/time/index'
import { rtClient, str, type PlaybackState, type RtClient, type RtEvent, type ServerInfoView, type TimeFrameView } from '@/net/rt'
import { apiGet, getToken } from '@/net/api'
import { DEMO_PUBLIC } from '@/lib/demo'
import { selectionStore } from './selection'
import { uiTickDue } from './uiTick'

export type { StepKind } from '@/engine/time/index'

export interface SegmentInfo {
  segment: number
  t0S: number
  t1S: number
  state: 'OPEN' | 'CLOSED' | 'CORRUPT'
  speedMax: number
  decimationS: number | null
  lineage: { epoch: number; t0S: number; t1S: number }[]
}
export interface Bookmark {
  id: string
  segment: number
  tS: number
  epoch: number
  label: string
  source: 'user' | 'local'
  createdBy: string | null
  createdWallNs: string
}
export interface PlaybackInfo {
  status: PlaybackState['status']
  speedMax: number
  loadedUntilS: number
  decimationS: number | null
  lineage: { epoch: number; t0S: number; t1S: number }[]
  dataStartS: number
  dataEndS: number
  warnings: string[]
  code: number | null
}
export interface TimelineNotice { seq: number; level: 'info' | 'warning'; key: string; code?: number }

export interface TimelineState {
  mode: 'live' | 'replay'
  state4: number
  replay: boolean
  stale: boolean
  rateActual: number
  rateRequested: number
  rtfLimited: boolean
  epoch: number
  runId: string | null
  segment: number
  segments: SegmentInfo[]
  rangeStartS: number
  rangeEndS: number
  /** the seen time (display only, <= 4 Hz on Tier S) */
  tDisplayS: number
  caps: { pausable: boolean; steppable: boolean; maxSpeed: number }
  /** operator or admin holding the write seat */
  canWrite: boolean
  playback: PlaybackInfo | null
  pending: { control: Control; target: number; deadlineMs: number } | null
  /** BUFFERING feedback of an unanswered seek (100 ms) */
  buffering: boolean
  view: { t0S: number; t1S: number }
  /** live view follows the playhead at the right end */
  followLive: boolean
  bookmarks: Bookmark[]
  markersVersion: number
  seriesVersion: number
  lastSeekMs: number | null
  /** rec.started / rec.stopped seen on the event channel */
  recording: boolean
  /** replay sidecars (.ovw, .evx) still loading */
  indexLoading: boolean
  notice: TimelineNotice | null
}

const initial = (): TimelineState => ({
  mode: 'live', state4: TS.STOPPED, replay: false, stale: true, rateActual: 1, rateRequested: 1, rtfLimited: false, epoch: -1,
  runId: null, segment: 0, segments: [], rangeStartS: 0, rangeEndS: 0, tDisplayS: 0,
  caps: { pausable: true, steppable: true, maxSpeed: 10 }, canWrite: false, playback: null, pending: null, buffering: false,
  view: { t0S: 0, t1S: 60 }, followLive: true, bookmarks: [], markersVersion: 0, seriesVersion: 0, lastSeekMs: null,
  recording: false, indexLoading: false, notice: null,
})

export const timelineStore = createAwrStore<TimelineState>('timeline', initial)

/** the track data M15's LfTimelineTrack reads (markers, columns, series, ranges, ticks, heroIdx) */
export const timelineTrack = new TrackModel()

/** realtime speeds (AWR-14 §6.17) and replay speeds (M12 §8.1) */
export const LIVE_RATES: readonly number[] = [0.25, 0.5, 1, 2, 5, 10]
export const REPLAY_RATES: readonly number[] = [0.1, 0.25, 0.5, 1, 2, 5, 10, 20]

// ------------------------------------------------------------------ module state
const pend = new PendingMachine()
const rtf = new RtfWatch()
let bound: RtClient | null = null
let explicitBinding = false
let unbind: (() => void)[] = []
let lastTime: TimeFrameView | null = null
let lastTimeAt = Number.NEGATIVE_INFINITY
let noticeSeq = 0
let notifier: ((n: TimelineNotice) => void) | null = null
let seekStartMs = Number.NaN
let seekTimer: ReturnType<typeof setTimeout> | null = null
let seekQueued = Number.NaN
let seekInFlight = false
let liveMinSeq = Number.POSITIVE_INFINITY
let lastHeroMs = Number.NEGATIVE_INFINITY
let lastSeriesMs = Number.NEGATIVE_INFINITY
let seriesAgent = -2
let hookedRuntime: unknown = null
const seriesPose = newDronePoseSoA(1)
/** recording gaps of the open replay segment (meta.json gaps, FR-027) */
let replayGaps: TrackRange[] = []
/** run:segment whose meta, sidecars and bookmarks are loaded (replays opened by another client or before this page) */
let replayCtxKey = ''
const seekLat: number[] = []

const now = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function notify(level: TimelineNotice['level'], key: string, code?: number): void {
  const n: TimelineNotice = { seq: ++noticeSeq, level, key, code }
  timelineStore.setState({ notice: n })
  notifier?.(n)
}

function pendingView(): TimelineState['pending'] {
  const p = pend.pending
  return p ? { control: p.control, target: p.target, deadlineMs: p.deadlineMs } : null
}

function clockSnapshot(): { state4: number; rate: number; tSimMs: number; epoch: number } {
  const c = timeRuntime()?.clock
  if (c && c.hasTime) return { state4: c.state4, rate: c.rate, tSimMs: c.tSimMs, epoch: c.epoch }
  const t = lastTime
  return t ? { state4: t.state & 0x0f, rate: t.rate, tSimMs: t.tSimMs, epoch: t.epoch & 0xffff } : { state4: TS.STOPPED, rate: 1, tSimMs: 0, epoch: -1 }
}

function currentGuards(): Guards {
  const s = timelineStore.getState()
  return guards({
    mode: s.mode, canWrite: s.canWrite, caps: s.caps, state4: s.state4, playbackStatus: s.playback?.status ?? null,
    speedMax: s.playback?.speedMax ?? 20,
  })
}

/** the guard table for the current state (disabled reasons as i18n keys; null = allowed) */
export function timelineGuards(): Guards {
  return currentGuards()
}

// ------------------------------------------------------------------ binding to the page RtClient
function onServerInfo(si: ServerInfoView): void {
  const s = timelineStore.getState()
  const mode = si.mode === 'replay' ? 'replay' : 'live'
  const caps = { pausable: si.clock?.pausable ?? true, steppable: si.clock?.steppable ?? true, maxSpeed: si.clock?.maxSpeed ?? 10 }
  const canWrite = si.role !== 'viewer' && si.seat === 'held'
  const patch: Partial<TimelineState> = {}
  if (mode !== s.mode) {
    patch.mode = mode
    if (mode === 'live') {
      patch.playback = null
      patch.segments = []
      patch.followLive = true
      replayCtxKey = ''
    } else if (!s.playback) {
      // live markers do not belong on a replay track; the replay context arrives with the next playbackState
      timelineTrack.clearMarkers()
      timelineTrack.clearSeries()
      patch.followLive = false
    }
  }
  if (caps.pausable !== s.caps.pausable || caps.steppable !== s.caps.steppable || caps.maxSpeed !== s.caps.maxSpeed) patch.caps = caps
  if (canWrite !== s.canWrite) patch.canWrite = canWrite
  if (mode === 'live' && si.runId && si.runId !== s.runId) {
    patch.runId = si.runId
    patch.segment = si.segment
  }
  if (Object.keys(patch).length) timelineStore.setState(patch)
  if (mode === 'replay' && !timelineStore.getState().playback) setTimeout(syncReplayState, 500)
  if (mode === 'live' && si.runId && si.runId !== s.runId) {
    void loadBookmarks(si.runId)
    void probeRecording()
  }
}

function onTime(t: TimeFrameView, recvMs: number): void {
  const prevEpoch = lastTime ? lastTime.epoch & 0xffff : -1
  lastTime = { state: t.state, epoch: t.epoch, rate: t.rate, tSimMs: t.tSimMs, tSrvMs: t.tSrvMs }
  lastTimeAt = recvMs
  const e = t.epoch & 0xffff
  if (prevEpoch >= 0 && e !== prevEpoch && timelineStore.getState().mode === 'live') {
    // scenario reset or restart: the live track starts over
    timelineTrack.clearMarkers()
    timelineTrack.clearSeries()
  }
  if (pend.pending) {
    const out = pend.observe(t.state & 0x0f, t.rate, t.tSimMs, e, now())
    if (out) settleOutcome(out)
  }
}

function onEvents(batch: readonly RtEvent[]): void {
  const roster = bound?.roster
  let recording: boolean | null = null
  for (const ev of batch) {
    if (ev.seq < liveMinSeq) liveMinSeq = ev.seq
    addEventMarker(ev, roster)
    if (ev.type === 'rec.started') recording = true
    else if (ev.type === 'rec.stopped') recording = false
    else if (ev.type === 'sim.clock' && typeof ev.data?.rate === 'number' && timelineStore.getState().mode === 'live') {
      const r = ev.data.rate
      if (!pend.pending || pend.pending.control !== 'speed') {
        if (r !== timelineStore.getState().rateRequested) timelineStore.setState({ rateRequested: r })
      }
    }
  }
  if (recording !== null && recording !== timelineStore.getState().recording) timelineStore.setState({ recording })
}

function addEventMarker(ev: RtEvent, roster: RtClient['roster'] | undefined): void {
  const cls = markerClassOf(ev.type, ev.level, ev.data)
  if (cls === 0) return
  const a = ev.uav ? roster?.agentNoOf(ev.uav) ?? -1 : -1
  timelineTrack.addMarker(ev.t_sim_ns / 1e6, ev.level, cls, a >= 0 ? a : 0xffff, ev.seq >>> 0, now())
}

function onPlaybackState(ps: PlaybackState): void {
  onPlaybackFinal(ps)
  const s = timelineStore.getState()
  // the reply to a command the gateway rejected (our request_id, a reason other than 213, e.g. 105 open while a replay is
  // open or 110 seek out of range) changes nothing on the server: keep the replay as it is, playbackCmd toasts the reason
  // (FX-WEB2; before, the banner then claimed the replay worker had stopped)
  if (ps.status === 'error' && ps.request_id && ps.code !== undefined && ps.code !== 213 && s.mode === 'replay' && s.playback) return
  if (ps.status === 'idle') {
    if (s.playback !== null || s.mode !== 'live') timelineStore.setState({ playback: null, mode: 'live', segments: [], followLive: true, buffering: false })
    return
  }
  const pb: PlaybackInfo = {
    status: ps.status,
    speedMax: ps.speed_max ?? s.playback?.speedMax ?? 20,
    loadedUntilS: ps.loaded_until_ns !== undefined ? ps.loaded_until_ns / 1e9 : s.playback?.loadedUntilS ?? 0,
    decimationS: ps.decimation_s ?? s.playback?.decimationS ?? null,
    lineage: ps.lineage ? ps.lineage.map((l) => ({ epoch: l.epoch, t0S: l.t_from_ns / 1e9, t1S: l.t_to_ns / 1e9 })) : s.playback?.lineage ?? [],
    dataStartS: ps.dataStart_ns !== undefined ? ps.dataStart_ns / 1e9 : s.playback?.dataStartS ?? 0,
    dataEndS: ps.dataEnd_ns !== undefined ? ps.dataEnd_ns / 1e9 : s.playback?.dataEndS ?? 0,
    warnings: ps.warnings ?? [],
    code: ps.code ?? null,
  }
  const patch: Partial<TimelineState> = { playback: pb }
  if (ps.status !== 'error' || s.mode === 'replay') patch.mode = 'replay'
  if (ps.run) patch.runId = ps.run
  if (ps.segment !== undefined) patch.segment = ps.segment
  if (typeof ps.speed === 'number' && ps.speed !== s.rateRequested) patch.rateRequested = ps.speed
  if (ps.did_seek) {
    patch.buffering = false
    pend.seekDone()
    patch.pending = pendingView()
  }
  timeRuntime()?.setBlockIntervalMs(pb.decimationS !== null ? pb.decimationS * 1000 : null)
  timelineStore.setState(patch)
  if (ps.status === 'error' && ps.code === 213) notify('warning', 'timeline.replayStopped', 213)
  // a replay this page did not open (another client, or opened before this page loaded): load its context once (FX-WEB2)
  if (ps.run && ps.status !== 'error' && `${ps.run}:${ps.segment ?? 0}` !== replayCtxKey) void loadReplayContext(ps.run, ps.segment ?? 0)
}

/** meta (segments, gaps), sidecars (.evx markers, .ovw series), bookmarks and the fitted view of a replay segment */
async function loadReplayContext(run: string, seg: number): Promise<void> {
  replayCtxKey = `${run}:${seg}`
  replayGaps = []
  timelineTrack.clearMarkers()
  timelineTrack.clearSeries()
  try {
    const meta = await apiGet<Record<string, unknown>>(`/api/runs/${encodeURIComponent(run)}`)
    if (replayCtxKey !== `${run}:${seg}`) return
    timelineStore.setState({ segments: segmentInfoOf(meta) })
    const gaps = Array.isArray(meta.gaps) ? (meta.gaps as Record<string, unknown>[]) : []
    replayGaps = gaps.filter((g) => Number(g.segment ?? -1) === seg)
      .map((g) => ({ kind: 'gap' as const, t0S: Number(g.t_from_ns ?? 0) / 1e9, t1S: Number(g.t_to_ns ?? 0) / 1e9 }))
  } catch {
    // meta unavailable: the playbackState fields are enough to play
  }
  const pb = timelineStore.getState().playback
  if (pb) timelineStore.setState({ view: { t0S: pb.dataStartS, t1S: Math.max(pb.dataEndS, pb.dataStartS + TIME_PARAMS.minSpanS) }, followLive: false })
  void loadSidecars(run, seg)
  void loadBookmarks(run)
}

/**
 * Fallback: the server is in replay mode but 500 ms after serverInfo this page still has no playbackState. The gateway
 * now sends one right after hello (FX-WEB2-to-M11 item 1, done by FX-GW: playback.on_hello), so this only covers a lost
 * message: a seat holder asks again with an idempotent speed command at the current rate; viewers keep the "syncing"
 * state until the next broadcast.
 */
function syncReplayState(): void {
  const s = timelineStore.getState()
  if (s.mode !== 'replay' || (s.playback && s.playback.dataEndS > 0) || !s.canWrite) return
  const rate = lastTime && lastTime.rate > 0 ? lastTime.rate : 1
  void playbackCmd('speed', { speed: rate })
}

function bind(rt: RtClient | null): void {
  if (rt === bound) return
  for (const off of unbind) off()
  unbind = []
  bound = rt
  lastTime = null
  liveMinSeq = Number.POSITIVE_INFINITY
  if (!rt) return
  unbind.push(rt.onServerInfo(onServerInfo), rt.onTime(onTime), rt.onEvents(onEvents), rt.onPlaybackState(onPlaybackState))
  if (rt.serverInfo) onServerInfo(rt.serverInfo)
  void backfillEvents()
}

/** older markers of the open recording segment (.evx) before the EventRing window (FR-018, recording on) */
async function backfillFromEvx(): Promise<void> {
  const s = timelineStore.getState()
  const run = s.runId
  if (!run || s.mode !== 'live') return
  try {
    const meta = await apiGet<Record<string, unknown>>(`/api/runs/${encodeURIComponent(run)}`)
    const segs = Array.isArray(meta.segments) ? (meta.segments as Record<string, unknown>[]) : []
    const open = segs.filter((x) => x.state === 'OPEN').pop()
    if (!open) return
    const token = await getToken()
    const r = await fetch(`/api/runs/${encodeURIComponent(run)}/segments/${String(Number(open.segment ?? 0)).padStart(3, '0')}.evx`,
      { headers: token ? { authorization: `Bearer ${token}` } : {} })
    if (!r.ok || timelineStore.getState().mode !== 'live') return
    const e = parseEvx(await r.arrayBuffer())
    const m = timelineTrack.markers
    const tMin = m.n ? m.t[0] : Number.POSITIVE_INFINITY
    let k = 0
    while (k < e.n && e.t[k] < tMin) k++
    if (k === 0) return
    const n = k + m.n
    const t = new Float64Array(n)
    const lv = new Uint8Array(n)
    const mk = new Uint8Array(n)
    const ag = new Uint16Array(n)
    const sq = new Uint32Array(n)
    t.set(e.t.subarray(0, k))
    lv.set(e.level.subarray(0, k))
    mk.set(e.marker.subarray(0, k))
    ag.set(e.agentNo.subarray(0, k))
    sq.set(e.mseq.subarray(0, k))
    t.set(m.t.subarray(0, m.n), k)
    lv.set(m.level.subarray(0, m.n), k)
    mk.set(m.marker.subarray(0, m.n), k)
    ag.set(m.agentNo.subarray(0, m.n), k)
    sq.set(m.mseq.subarray(0, m.n), k)
    timelineTrack.loadEvx(n, t, lv, mk, ag, sq)
  } catch {
    // no recording or no runs REST: the EventRing backfill is all there is
  }
}

/** GET /api/events backfill of the markers (late or reconnecting clients, FR-018); events seen live are skipped */
async function backfillEvents(): Promise<void> {
  const rt = bound
  let since = 0
  let recording: boolean | null = null
  try {
    for (let page = 0; page < 500; page++) {
      const r = await apiGet<{ items: RtEvent[]; next_since: number }>(`/api/events?since=${since}&limit=1000`)
      if (rt !== bound) return
      for (const ev of r.items) {
        if (ev.seq >= liveMinSeq) continue
        addEventMarker(ev, rt?.roster)
        if (ev.type === 'rec.started' || ev.type === 'rec.stopped') recording = ev.type === 'rec.started'
      }
      if (!r.items.length || r.next_since <= since) break
      since = r.next_since
    }
  } catch {
    // no gateway (FakeSource pages) or the ring moved on (410): the live channel still fills the track
  }
  if (recording !== null && rt === bound && liveMinSeq === Number.POSITIVE_INFINITY) timelineStore.setState({ recording })
  // a recorder that auto-started with the scenario may have published rec.started before the gateway subscribed: the
  // run's meta (an OPEN segment) is the authority then (FX-WEB2)
  if (rt === bound && recording === null && !timelineStore.getState().recording) await probeRecording()
  if (rt === bound && timelineStore.getState().recording) await backfillFromEvx()
}

/** recording state from GET /api/runs/{run}: an OPEN segment of the current live run means "recording" */
async function probeRecording(): Promise<void> {
  const s = timelineStore.getState()
  if (!s.runId || s.mode !== 'live' || DEMO_PUBLIC) return  // the public demo runs no recorder and offers no runs REST (ADR-083)
  try {
    const meta = await apiGet<Record<string, unknown>>(`/api/runs/${encodeURIComponent(s.runId)}`)
    const segs = Array.isArray(meta.segments) ? (meta.segments as Record<string, unknown>[]) : []
    const open = segs.some((x) => x.state === 'OPEN')
    if (open !== timelineStore.getState().recording && timelineStore.getState().mode === 'live') timelineStore.setState({ recording: open })
  } catch {
    // no recording for this run (404) or no runs REST
  }
}

// ------------------------------------------------------------------ periodic update (overlay phase)
function rangeRanges(pb: PlaybackInfo | null, segs: SegmentInfo[], seg: number): TrackRange[] {
  const out: TrackRange[] = []
  if (!pb) return out
  const info = segs.find((x) => x.segment === seg)
  const lin = pb.lineage.length ? pb.lineage : info?.lineage ?? []
  for (let i = 1; i < lin.length; i++) {
    // a later epoch starting before the previous one ended re-ran [restored, last]
    const prev = lin[i - 1]
    if (lin[i].t0S < prev.t1S) out.push({ kind: 'rerun', t0S: lin[i].t0S, t1S: prev.t1S })
  }
  if (pb.decimationS !== null && pb.decimationS > TIME_PARAMS.blockIntervalMs / 1000 + 1e-9) out.push({ kind: 'decimated', t0S: pb.dataStartS, t1S: pb.dataEndS })
  if (pb.loadedUntilS > 0) out.push({ kind: 'loaded', t0S: timelineStore.getState().tDisplayS, t1S: pb.loadedUntilS })
  for (const g of replayGaps) out.push(g)
  return out
}

/** one store update per call; exported for tests (nowMs = performance.now()) */
export function tickTimeline(nowMs: number = now()): boolean {
  if (!explicitBinding) bind(rtClient())
  const rtm = timeRuntime()
  if (rtm && hookedRuntime !== rtm) {
    hookedRuntime = rtm
    rtm.onEpochData = (_e, t) => {
      if (Number.isNaN(seekStartMs)) return
      const ms = t - seekStartMs
      seekStartMs = Number.NaN
      recordSeek(ms)
    }
  }
  const s = timelineStore.getState()
  const patch: Partial<TimelineState> = {}
  let state4: number
  let rate: number
  let epoch: number
  let stale: boolean
  let replay: boolean
  let tDisp: number
  let simNowS: number
  if (rtm && rtm.clock.hasTime) {
    const c = rtm.clock
    state4 = c.state4
    rate = c.rate
    epoch = c.epoch
    stale = c.stale
    replay = c.replay
    tDisp = c.tRenderS()
    simNowS = c.simNowS()
  } else if (lastTime) {
    state4 = lastTime.state & 0x0f
    rate = lastTime.rate
    epoch = lastTime.epoch & 0xffff
    stale = nowMs - lastTimeAt > TIME_PARAMS.staleMs
    replay = (lastTime.state & 0x80) !== 0
    tDisp = lastTime.tSimMs / 1000
    simNowS = tDisp
  } else {
    state4 = s.state4
    rate = s.rateActual
    epoch = s.epoch
    stale = true
    replay = s.replay
    tDisp = s.tDisplayS
    simNowS = tDisp
  }
  // an optimistic seek holds the playhead at its target until the new epoch shows up
  if (pend.pending?.control === 'seek' && s.mode === 'replay') tDisp = pend.pending.target / 1000
  if (state4 !== s.state4) patch.state4 = state4
  if (rate !== s.rateActual) patch.rateActual = rate
  if (epoch !== s.epoch) patch.epoch = epoch
  if (stale !== s.stale) patch.stale = stale
  if (replay !== s.replay) patch.replay = replay
  if (Math.abs(tDisp - s.tDisplayS) > 1e-6) patch.tDisplayS = tDisp
  // pending timeout and TIME confirmation
  if (pend.pending) {
    const out = pend.observe(state4, rate, rtm?.clock.tSimMs ?? lastTime?.tSimMs ?? 0, epoch, nowMs)
    if (out) settleOutcome(out, patch)
  }
  // RTF badge (live)
  const lim = s.mode === 'live' ? rtf.update(rate, s.rateRequested, state4 === TS.PLAYING, nowMs) : false
  if (lim !== s.rtfLimited) patch.rtfLimited = lim
  // range and view
  const pb = s.playback
  const r0 = s.mode === 'replay' && pb ? pb.dataStartS : 0
  const r1 = s.mode === 'replay' && pb ? pb.dataEndS : Math.max(simNowS, tDisp)
  if (r0 !== s.rangeStartS) patch.rangeStartS = r0
  if (Math.abs(r1 - s.rangeEndS) > 1e-6) patch.rangeEndS = r1
  if (s.mode === 'live' && s.followLive) {
    const span = Math.max(TIME_PARAMS.minSpanS, s.view.t1S - s.view.t0S)
    const t1 = Math.max(tDisp, span)
    if (Math.abs(t1 - s.view.t1S) > 1e-6) patch.view = { t0S: t1 - span, t1S: t1 }
  }
  // selected vehicle altitude (4 Hz)
  sampleSeries(nowMs, rtm, tDisp)
  // ranges (replay)
  if (s.mode === 'replay') {
    const rs = rangeRanges(pb, s.segments, s.segment)
    if (rs.length !== timelineTrack.ranges.length || rs.some((x, i) => x.kind !== timelineTrack.ranges[i].kind || x.t0S !== timelineTrack.ranges[i].t0S || x.t1S !== timelineTrack.ranges[i].t1S)) timelineTrack.setRanges(rs)
  } else if (timelineTrack.ranges.length) timelineTrack.setRanges([])
  // one red
  if (nowMs - lastHeroMs >= INPUT.redEvalIntervalMs) {
    lastHeroMs = nowMs
    timelineTrack.evalHero(nowMs)
  }
  if (timelineTrack.version !== s.markersVersion) patch.markersVersion = timelineTrack.version
  if (timelineTrack.version !== s.seriesVersion) patch.seriesVersion = timelineTrack.version
  perfTime.focusLowLatency = rtm?.clock.focusLowLatency ?? false
  if (Object.keys(patch).length === 0) return false
  timelineStore.setState(patch)
  return true
}

function sampleSeries(nowMs: number, rtm: ReturnType<typeof timeRuntime>, tS: number): void {
  if (nowMs - lastSeriesMs < 1000 / TIME_PARAMS.seriesHz) return
  lastSeriesMs = nowMs
  if (timelineStore.getState().mode === 'replay') return // replay series comes from the .ovw track
  const id = selectionStore.getState().primary
  const a = id !== null ? bound?.roster.agentNoOf(id) ?? -1 : -1
  if (a !== seriesAgent) {
    seriesAgent = a
    timelineTrack.clearSeries()
  }
  if (a < 0 || !rtm) return
  if (rtm.interp.sampleOne(a, tS, seriesPose, 0)) timelineTrack.pushSeries(tS * 1000, seriesPose.pos[2])
}

let lastSettled: { control: Control; fromRate: number } | null = null
function settleOutcome(out: 'confirmed' | 'timeout' | 'epoch', patch?: Partial<TimelineState>): void {
  const target = patch ?? {}
  target.pending = pendingView()
  if (out === 'timeout') {
    const s = timelineStore.getState()
    const p = lastSettled
    if (p?.control === 'speed' && s.mode === 'live') target.rateRequested = p.fromRate
    notify('warning', 'timeline.noConfirm')
  }
  if (!patch) timelineStore.setState(target)
}

function recordSeek(ms: number): void {
  seekLat.push(ms)
  if (seekLat.length > 50) seekLat.shift()
  const sorted = [...seekLat].sort((a, b) => a - b)
  perfTime.seekMs = ms
  perfTime.seekP95Ms = sorted[Math.min(sorted.length - 1, Math.floor(0.95 * sorted.length))]
  timelineStore.setState({ lastSeekMs: ms })
}

// published on the shared UI tick (stores/uiTick.ts, ADR-066): its cadence equals TIME_PARAMS.storeHzS / storeHzBA (4 / 10 Hz)
loop.register('overlay', 'timeline.s', (ctx) => void (uiTickDue(ctx) && tickTimeline()), { tiers: ['S'] })
loop.register('overlay', 'timeline.ba', (ctx) => void (uiTickDue(ctx) && tickTimeline()), { tiers: ['A', 'B'] })

// ------------------------------------------------------------------ actions
function startPending(control: Control, target: number, fromRate: number = timelineStore.getState().rateRequested): void {
  const snap = clockSnapshot()
  pend.start(control, target, now(), { ...snap, rate: fromRate })
  lastSettled = { control, fromRate }
  timelineStore.setState({ pending: pendingView() })
}

function rejected(code: number): void {
  const p = pend.settle(false)
  const s = timelineStore.getState()
  const patch: Partial<TimelineState> = { pending: null }
  if (p?.control === 'speed' && s.mode === 'live') patch.rateRequested = p.fromRate
  timelineStore.setState(patch)
  notify('warning', reasonKey(code), code)
}

/** live clock call with pending handling (sim/play, sim/pause, sim/step, sim/speed) */
function liveCall(service: string, args: object, control: Control, target: number, fromRate?: number): void {
  const rt = bound ?? rtClient()
  if (!rt) return
  startPending(control, target, fromRate)
  const h = rt.call(service, args)
  h.result.then((r) => {
    if (r.status === 'rejected' || r.status === 'failed' || r.status === 'timeout') rejected(r.code)
    else if (r.status === 'succeeded' && (control === 'speed' || control === 'step')) {
      pend.settle(true)
      timelineStore.setState({ pending: pendingView() })
    }
  }, () => rejected(213))
}

/**
 * Playback commands are serialised (the gateway rejects a command while the previous one runs) and resolve with the
 * final playbackState of their request_id: seek waits for did_seek, open for the state after `opening` (the gateway
 * first broadcasts the intermediate buffering or opening state with the same request_id). 10 s without it: 213.
 */
let pbChain: Promise<unknown> = Promise.resolve()
let pbSeq = 0
const pbFinals = new Map<string, { cmd: string; ok: (s: PlaybackState) => void }>()
const pbIsFinal = (cmd: string, s: PlaybackState): boolean =>
  s.status === 'error' || (cmd === 'seek' ? s.did_seek === true : cmd === 'open' ? s.status !== 'opening' : true)

function onPlaybackFinal(ps: PlaybackState): void {
  const id = ps.request_id
  if (!id) return
  const w = pbFinals.get(id)
  if (w && pbIsFinal(w.cmd, ps)) {
    pbFinals.delete(id)
    w.ok(ps)
  }
}

function playbackCmd(cmd: 'play' | 'pause' | 'speed' | 'seek' | 'open' | 'close', args: Record<string, unknown> = {}): Promise<PlaybackState | null> {
  const run = async (): Promise<PlaybackState | null> => {
    const rt = bound ?? rtClient()
    if (!rt) return null
    const id = `tl-${Date.now().toString(36)}-${++pbSeq}`
    let timer: ReturnType<typeof setTimeout> | null = null
    const final = new Promise<PlaybackState>((ok) => {
      pbFinals.set(id, { cmd, ok })
      timer = setTimeout(() => {
        pbFinals.delete(id)
        ok({ status: 'error', code: 213, request_id: id })
      }, 10_000)
    })
    try {
      const first = await rt.playback(cmd, { ...args, request_id: id })
      const st = pbIsFinal(cmd, first) ? first : await final
      pbFinals.delete(id)
      if (st.status === 'error') {
        notify('warning', reasonKey(st.code ?? 213), st.code ?? 213)
        if (cmd === 'seek') {
          pend.seekDone()
          clearSeekTimer()
          timelineStore.setState({ pending: pendingView(), buffering: false })
        }
      }
      return st
    } catch {
      pbFinals.delete(id)
      notify('warning', reasonKey(213), 213)
      return null
    } finally {
      if (timer !== null) clearTimeout(timer)
    }
  }
  const p = pbChain.then(run, run)
  pbChain = p.catch(() => null)
  return p
}

function clearSeekTimer(): void {
  if (seekTimer !== null) clearTimeout(seekTimer)
  seekTimer = null
}

function doSeek(tS: number): void {
  const s = timelineStore.getState()
  const pb = s.playback
  if (s.mode !== 'replay' || !pb) return
  const t = Math.min(pb.dataEndS, Math.max(pb.dataStartS, tS))
  if (seekInFlight) {
    seekQueued = t // coalesce: the final target goes out when the current seek completes (M12 §6.5)
    pend.start('seek', t * 1000, now(), clockSnapshot())
    timelineStore.setState({ tDisplayS: t, pending: pendingView() })
    return
  }
  seekInFlight = true
  seekStartMs = now()
  pend.start('seek', t * 1000, seekStartMs, clockSnapshot())
  timelineStore.setState({ tDisplayS: t, pending: pendingView() })
  clearSeekTimer()
  seekTimer = setTimeout(() => {
    seekTimer = null
    if (pend.pending?.control === 'seek') timelineStore.setState({ buffering: true })
  }, TIME_PARAMS.bufferingFeedbackMs)
  void playbackCmd('seek', { seek_ns: Math.round(t * 1e9) }).then((st) => {
    seekInFlight = false
    clearSeekTimer()
    if (st && st.status !== 'error' && !timeRuntime()) recordSeek(now() - seekStartMs) // no viewport: reply time
    if (!Number.isNaN(seekQueued)) {
      const q = seekQueued
      seekQueued = Number.NaN
      doSeek(q)
      return
    }
    pend.seekDone()
    timelineStore.setState({ pending: pendingView(), buffering: false })
  })
}

function localKey(run: string): string {
  return `awr.bm.${run}`
}
function readLocal(run: string): Bookmark[] {
  try {
    const raw = globalThis.localStorage?.getItem(localKey(run))
    const v = raw ? (JSON.parse(raw) as { items?: unknown[] }) : null
    return Array.isArray(v?.items) ? (v.items as Record<string, unknown>[]).map((x) => fromWire(x, 'local')) : []
  } catch {
    return []
  }
}
function writeLocal(run: string, items: Bookmark[]): void {
  try {
    globalThis.localStorage?.setItem(localKey(run), JSON.stringify({ schema: 'awr.run.bookmarks.v1', items: items.filter((b) => b.source === 'local').map(toWire) }))
  } catch {
    // storage unavailable: local bookmarks live for this page only
  }
}
function toWire(b: Bookmark): Record<string, unknown> {
  return { id: b.id, segment: b.segment, t_sim_ns: Math.round(b.tS * 1e9), label: b.label, created_wall_ns: b.createdWallNs, principal_id: b.createdBy ?? undefined }
}
function fromWire(x: Record<string, unknown>, source: 'user' | 'local'): Bookmark {
  return {
    id: str(x.id), segment: Number(x.segment ?? 0), tS: Number(x.t_sim_ns ?? 0) / 1e9, epoch: Number(x.epoch ?? 0),
    label: str(x.label), source, createdBy: typeof x.principal_id === 'string' ? x.principal_id : null,
    createdWallNs: str(x.created_wall_ns, '0'),
  }
}
function setBookmarks(items: Bookmark[]): void {
  const sorted = items.slice().sort((a, b) => a.tS - b.tS)
  timelineStore.setState({ bookmarks: sorted })
  const seg = timelineStore.getState().segment
  timelineTrack.setBookmarks(sorted.filter((b) => b.segment === seg).map((b) => b.tS))
}

async function apiSend<T>(method: 'POST' | 'PATCH' | 'DELETE', path: string, body?: unknown): Promise<T | null> {
  const token = await getToken()
  const headers: Record<string, string> = { accept: 'application/json' }
  if (token) headers.authorization = `Bearer ${token}`
  if (body !== undefined) headers['content-type'] = 'application/json'
  const r = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined })
  if (!r.ok) {
    let code = r.status
    try {
      const p = (await r.json()) as { code?: number }
      if (typeof p.code === 'number') code = p.code
    } catch {
      // no problem body
    }
    throw Object.assign(new Error(`${method} ${path} ${r.status}`), { code })
  }
  return r.status === 204 ? null : ((await r.json()) as T)
}

async function loadBookmarks(run: string): Promise<void> {
  let shared: Bookmark[] = []
  // the public demo offers no runs REST (ADR-083): local bookmarks only
  if (!DEMO_PUBLIC) {
    try {
      const r = await apiGet<{ items: Record<string, unknown>[] }>(`/api/runs/${encodeURIComponent(run)}/bookmarks`)
      shared = (r.items ?? []).map((x) => fromWire(x, 'user'))
    } catch {
      shared = []
    }
  }
  if (timelineStore.getState().runId !== run) return
  setBookmarks([...shared, ...readLocal(run)])
}

async function loadSidecars(run: string, seg: number): Promise<void> {
  const k = String(seg).padStart(3, '0')
  timelineStore.setState({ indexLoading: true })
  try {
    const fetchBin = async (ext: string): Promise<ArrayBuffer | null> => {
      const token = await getToken()
      const r = await fetch(`/api/runs/${encodeURIComponent(run)}/segments/${k}.${ext}`, { headers: token ? { authorization: `Bearer ${token}` } : {} })
      return r.ok ? r.arrayBuffer() : null
    }
    const [evxBuf, ovwBuf] = await Promise.all([fetchBin('evx'), fetchBin('ovw')])
    if (evxBuf) {
      const e = parseEvx(evxBuf)
      timelineTrack.loadEvx(e.n, e.t, e.level, e.marker, e.agentNo, e.mseq)
    }
    if (ovwBuf) {
      const o = parseOvw(ovwBuf)
      // series: the first marked track's altitude (1 Hz)
      if (o.nTracks > 0 && o.nBins > 0) {
        const t = new Float64Array(o.nBins)
        const v = new Float32Array(o.nBins)
        let n = 0
        for (let b = 0; b < o.nBins; b++) {
          const z = o.trackPos[3 * (b * o.nTracks) + 2]
          if (!Number.isFinite(z)) continue
          t[n] = ovwBinMs(o, b) + o.binNs / 2e6
          v[n] = z
          n++
        }
        timelineTrack.setSeries(t, v, n)
      }
    }
  } catch {
    // sidecars are optional: the track shows the floor only
  } finally {
    timelineStore.setState({ indexLoading: false })
  }
}

function segmentInfoOf(meta: Record<string, unknown>): SegmentInfo[] {
  const segs = Array.isArray(meta.segments) ? (meta.segments as Record<string, unknown>[]) : []
  return segs.map((x) => {
    const bps = Number(x.bytes_per_sim_s ?? 0)
    const dec = x.decimation as { active?: boolean; sim_resolution_s?: number } | undefined
    const lin = Array.isArray(x.lineage) ? (x.lineage as Record<string, unknown>[]) : []
    return {
      segment: Number(x.segment ?? 0), t0S: Number(x.t_start_ns ?? 0) / 1e9, t1S: Number(x.t_end_ns ?? x.t_start_ns ?? 0) / 1e9,
      state: (x.state as SegmentInfo['state']) ?? 'OPEN',
      speedMax: Number(x.speed_max ?? (bps > 0 ? Math.min(20, 67108864 / bps) : 20)),
      decimationS: dec?.active ? Number(dec.sim_resolution_s ?? 0.2) : null,
      lineage: lin.map((l) => ({ epoch: Number(l.epoch ?? 0), t0S: Number(l.t_start_ns ?? 0) / 1e9, t1S: l.t_end_ns == null ? Number.POSITIVE_INFINITY : Number(l.t_end_ns) / 1e9 })),
    }
  })
}

export const timeline = {
  play(): void {
    const s = timelineStore.getState()
    const g = currentGuards()
    if (g.play) return
    if (s.mode === 'replay') {
      startPending('play', TS.PLAYING)
      void playbackCmd('play').then((st) => {
        if (!st || st.status === 'error') {
          pend.clear()
          timelineStore.setState({ pending: null })
        }
      })
      return
    }
    liveCall('sim/play', {}, 'play', TS.PLAYING)
  },
  pause(): void {
    const s = timelineStore.getState()
    if (currentGuards().play) return
    if (s.mode === 'replay') {
      startPending('pause', TS.PAUSED)
      void playbackCmd('pause').then((st) => {
        if (!st || st.status === 'error') {
          pend.clear()
          timelineStore.setState({ pending: null })
        }
      })
      return
    }
    liveCall('sim/pause', {}, 'pause', TS.PAUSED)
  },
  togglePlay(): void {
    const s = timelineStore.getState()
    if (s.mode === 'replay' && s.state4 === TS.ENDED && s.playback) {
      // "play from the start" (tl.replay)
      doSeek(s.playback.dataStartS)
      timeline.play()
      return
    }
    const target = pend.pending?.control === 'play' ? TS.PLAYING : pend.pending?.control === 'pause' ? TS.PAUSED : clockSnapshot().state4
    if (target === TS.PLAYING || target === TS.BUFFERING) timeline.pause()
    else timeline.play()
  },
  setRate(r: number): void {
    const s = timelineStore.getState()
    if (!(r > 0) || currentGuards().speed(r)) return
    if (s.mode === 'replay') {
      timelineStore.setState({ rateRequested: r })
      void playbackCmd('speed', { speed: r }).then((st) => {
        if (st && st.status !== 'error') {
          const sp = typeof st.speed === 'number' ? st.speed : r
          timelineStore.setState({ rateRequested: sp })
          if (st.warnings?.includes('SPEED_CLAMPED')) notify('info', 'timeline.speedClamped')
        }
      })
      return
    }
    const prev = s.rateRequested
    timelineStore.setState({ rateRequested: r })
    liveCall('sim/speed', { rate: r }, 'speed', r, prev)
  },
  step(k: StepKind): void {
    const s = timelineStore.getState()
    if (currentGuards().step) return
    if (s.mode === 'replay') {
      const pb = s.playback
      if (!pb) return
      const from = pend.pending?.control === 'seek' ? pend.pending.target / 1000 : s.tDisplayS
      doSeek(replayStepTarget(k, from, pb.decimationS ?? TIME_PARAMS.blockIntervalMs / 1000, pb.dataStartS, pb.dataEndS))
      return
    }
    const ticks = LIVE_STEP_TICKS[k]
    if (!ticks) return
    const t0 = clockSnapshot().tSimMs
    liveCall('sim/step', { ticks }, 'step', t0 + liveStepMs(k))
  },
  seek(tS: number): void {
    if (currentGuards().seek) return
    doSeek(tS)
  },
  zoom(t0S: number, t1S: number): void {
    const s = timelineStore.getState()
    const lo = s.mode === 'replay' ? s.rangeStartS : 0
    const hi = Math.max(s.rangeEndS, lo + TIME_PARAMS.minSpanS)
    let a = Math.min(t0S, t1S)
    let b = Math.max(t0S, t1S)
    if (b - a < TIME_PARAMS.minSpanS) {
      const c = (a + b) / 2
      a = c - TIME_PARAMS.minSpanS / 2
      b = c + TIME_PARAMS.minSpanS / 2
    }
    const span = Math.min(b - a, hi - lo)
    if (a < lo) {
      a = lo
      b = lo + span
    }
    if (b > hi) {
      b = hi
      a = hi - span
    }
    timelineStore.setState({ view: { t0S: a, t1S: b }, followLive: s.mode === 'live' && b >= s.rangeEndS - 1e-3 })
  },
  fit(): void {
    const s = timelineStore.getState()
    const lo = s.mode === 'replay' ? s.rangeStartS : 0
    const hi = Math.max(s.rangeEndS, lo + TIME_PARAMS.minSpanS)
    timelineStore.setState({ view: { t0S: lo, t1S: hi }, followLive: s.mode === 'live' })
  },
  pan(dS: number): void {
    const v = timelineStore.getState().view
    timeline.zoom(v.t0S + dS, v.t1S + dS)
  },
  jumpMarker(dir: 1 | -1): void {
    const s = timelineStore.getState()
    if (s.mode !== 'replay') return
    const t = timelineTrack.nextMarkTime(pend.pending?.control === 'seek' ? pend.pending.target / 1000 : s.tDisplayS, dir)
    if (Number.isFinite(t)) timeline.seek(t)
  },
  async addBookmark(label = '', tS?: number): Promise<Bookmark | null> {
    const s = timelineStore.getState()
    const run = s.runId
    if (!run) return null
    if (s.bookmarks.length >= TIME_PARAMS.bookmarksCap) {
      notify('warning', reasonKey(462), 462)
      return null
    }
    const t = tS ?? s.tDisplayS
    const text = sanitizeText(label, TIME_PARAMS.bookmarkLabelMax)
    const wall = String(BigInt(Date.now()) * 1_000_000n)
    if (s.canWrite) {
      try {
        const r = await apiSend<{ id: string }>('POST', `/api/runs/${encodeURIComponent(run)}/bookmarks`, { segment: s.segment, t_sim_ns: Math.round(t * 1e9), label: text })
        const b: Bookmark = { id: r?.id ?? '', segment: s.segment, tS: t, epoch: Math.max(0, s.epoch), label: text, source: 'user', createdBy: null, createdWallNs: wall }
        setBookmarks([...timelineStore.getState().bookmarks, b])
        return b
      } catch (e) {
        notify('warning', reasonKey((e as { code?: number }).code ?? 213), (e as { code?: number }).code)
        return null
      }
    }
    const b: Bookmark = { id: `bm-${Math.floor(Date.now()).toString(16)}-${(noticeSeq++).toString(16)}`, segment: s.segment, tS: t, epoch: Math.max(0, s.epoch), label: text, source: 'local', createdBy: null, createdWallNs: wall }
    const items = [...s.bookmarks, b]
    writeLocal(run, items)
    setBookmarks(items)
    return b
  },
  async editBookmark(id: string, label: string): Promise<void> {
    const s = timelineStore.getState()
    const b = s.bookmarks.find((x) => x.id === id)
    if (!b || !s.runId) return
    const text = sanitizeText(label, TIME_PARAMS.bookmarkLabelMax)
    if (b.source === 'user') {
      try {
        await apiSend('PATCH', `/api/runs/${encodeURIComponent(s.runId)}/bookmarks/${encodeURIComponent(id)}`, { label: text })
      } catch (e) {
        notify('warning', reasonKey((e as { code?: number }).code ?? 213), (e as { code?: number }).code)
        return
      }
    }
    const items = timelineStore.getState().bookmarks.map((x) => (x.id === id ? { ...x, label: text } : x))
    if (b.source === 'local') writeLocal(s.runId, items)
    setBookmarks(items)
  },
  async removeBookmark(id: string): Promise<void> {
    const s = timelineStore.getState()
    const b = s.bookmarks.find((x) => x.id === id)
    if (!b || !s.runId) return
    if (b.source === 'user') {
      try {
        await apiSend('DELETE', `/api/runs/${encodeURIComponent(s.runId)}/bookmarks/${encodeURIComponent(id)}`)
      } catch (e) {
        notify('warning', reasonKey((e as { code?: number }).code ?? 213), (e as { code?: number }).code)
        return
      }
    }
    const items = timelineStore.getState().bookmarks.filter((x) => x.id !== id)
    if (b.source === 'local') writeLocal(s.runId, items)
    setBookmarks(items)
  },
  async openReplay(run: string, seg = 0, tS?: number): Promise<boolean> {
    replayGaps = []
    const prevKey = replayCtxKey
    replayCtxKey = `${run}:${seg}` // this call loads the context itself (the broadcast playbackState must not repeat it)
    const st = await playbackCmd('open', { run, segment: seg })
    if (!st || st.status === 'error') {
      replayCtxKey = prevKey
      return false
    }
    timelineTrack.clearMarkers()
    timelineTrack.clearSeries()
    timelineStore.setState({ runId: run, segment: seg, mode: 'replay', followLive: false })
    try {
      const meta = await apiGet<Record<string, unknown>>(`/api/runs/${encodeURIComponent(run)}`)
      timelineStore.setState({ segments: segmentInfoOf(meta) })
      const gaps = Array.isArray(meta.gaps) ? (meta.gaps as Record<string, unknown>[]) : []
      replayGaps = gaps.filter((g) => Number(g.segment ?? -1) === seg)
        .map((g) => ({ kind: 'gap' as const, t0S: Number(g.t_from_ns ?? 0) / 1e9, t1S: Number(g.t_to_ns ?? 0) / 1e9 }))
    } catch {
      // meta unavailable: the playbackState fields are enough to play
    }
    const pb = timelineStore.getState().playback
    if (pb) timelineStore.setState({ view: { t0S: pb.dataStartS, t1S: Math.max(pb.dataEndS, pb.dataStartS + TIME_PARAMS.minSpanS) } })
    void loadSidecars(run, seg)
    void loadBookmarks(run)
    if (tS !== undefined && Number.isFinite(tS)) doSeek(tS)
    return true
  },
  async closeReplay(): Promise<void> {
    await playbackCmd('close')
    replayCtxKey = ''
    timelineTrack.clearMarkers()
    timelineTrack.clearSeries()
    timelineStore.setState({ mode: 'live', playback: null, segments: [], followLive: true, buffering: false })
    void backfillEvents()
  },
  /** M15 registers its toast function here (stores do not import ui/**) */
  setNotifier(fn: ((n: TimelineNotice) => void) | null): void {
    notifier = fn
  },
  /** HoverCard facts at the moment of reading (engine facade; never stored) */
  clockFacts(): { simNowS: number; tRenderS: number; dGlobalMs: number; dFocusMs: number; rateActual: number; rateRequested: number; epoch: number; wallMs: number | null } {
    const c = timeRuntime()?.clock
    const s = timelineStore.getState()
    return {
      simNowS: c ? c.simNowS() : s.tDisplayS, tRenderS: c ? c.tRenderS() : s.tDisplayS, dGlobalMs: c?.dGlobalMs ?? 0, dFocusMs: c?.dFocusMs ?? 0,
      rateActual: s.rateActual, rateRequested: s.rateRequested, epoch: s.epoch, wallMs: s.mode === 'live' ? Date.now() : null,
    }
  },
}

export function useTimeline<T>(sel: (s: TimelineState) => T): T {
  return useStore(timelineStore, sel)
}

/** tests: forget the binding and module state */
export function resetTimeline(): void {
  bind(null)
  explicitBinding = false
  pend.clear()
  rtf.reset()
  clearSeekTimer()
  seekStartMs = Number.NaN
  seekQueued = Number.NaN
  seekInFlight = false
  seekLat.length = 0
  lastHeroMs = Number.NEGATIVE_INFINITY
  lastSeriesMs = Number.NEGATIVE_INFINITY
  seriesAgent = -2
  hookedRuntime = null
  timelineTrack.clearMarkers()
  timelineTrack.clearSeries()
  timelineTrack.setRanges([])
  replayCtxKey = ''
  timelineStore.setState(initial())
}

/** tests: bind a stub client explicitly */
export function bindTimeline(rt: RtClient | null): void {
  explicitBinding = rt !== null
  bind(rt)
}

// test builds (VITE_AWR_TEST_SWITCHES=1): Playwright specs of apps/web/perf/m12 drive the store and read the track model
if (TEST_SWITCHES && typeof window !== 'undefined') {
  ;(window as unknown as { __timeline?: unknown }).__timeline = { store: timelineStore, actions: timeline, track: timelineTrack, guards: timelineGuards }
}
