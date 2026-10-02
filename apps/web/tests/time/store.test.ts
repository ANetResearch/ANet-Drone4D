// stores/timeline.ts (M12 §7.1; FR-014 to FR-016, FR-018 to FR-020, FR-025, FR-026, FR-028; M12-AC-014, AC-016,
// AC-020, AC-026 store part) against a stub RtClient: live transport calls with pending confirmation, rejection texts and
// the 1 s timeout; viewer greying; step ticks; RTF badge; <= 4 store writes per second at Tier S cadence; replay seek
// (optimistic playhead, BUFFERING after 100 ms, coalesced steps, did_seek), speed clamping, marker jumps, event markers
// and the one red, local bookmarks for viewers.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CallResult, PlaybackState, RtClient, RtEvent, ServerInfoView, TimeFrameView } from '@/net/rt/types'
import { TIME_STATE as TS } from '@/engine/time/index'
import { bindTimeline, resetTimeline, tickTimeline, timeline, timelineStore, timelineTrack } from '@/stores/timeline'

const RUN = 'r20260929-010203-abcd'
interface Stub {
  rt: RtClient
  calls: { service: string; args: Record<string, unknown> }[]
  pb: { cmd: string; args: Record<string, unknown> }[]
  time(state: number, tSimMs: number, rate?: number, epoch?: number): void
  events(ev: Partial<RtEvent>[]): void
  playbackState(s: PlaybackState): void
  si(p: Partial<ServerInfoView>): void
  callResult: (service: string) => Promise<CallResult>
  pbReply: (cmd: string, args: Record<string, unknown>) => Promise<PlaybackState>
}

function stub(): Stub {
  const L = { si: [] as ((s: ServerInfoView) => void)[], time: [] as ((t: TimeFrameView, r: number) => void)[], ev: [] as ((b: readonly RtEvent[]) => void)[], pb: [] as ((s: PlaybackState) => void)[] }
  let info = {
    sessionId: 's', connId: 'c', worldId: 'shenzhen', contentVersion: 'x', runId: RUN, segment: 0, mode: 'live', role: 'operator', seat: 'held',
    clock: { mode: 'lockstep', pausable: true, maxSpeed: 10, steppable: true }, window: 6, layouts: {}, principal: 'p', capabilities: [],
  } as unknown as ServerInfoView
  const S: Stub = {
    calls: [], pb: [],
    callResult: async () => ({ id: 'x', status: 'succeeded', code: 0 }),
    pbReply: async (cmd) => ({ status: cmd === 'play' ? 'playing' : 'paused', ...(cmd === 'seek' ? { did_seek: true } : {}) }),
    rt: undefined as unknown as RtClient,
    time(state, tSimMs, rate = 1, epoch = 1) {
      for (const cb of L.time) cb({ state, epoch, rate, tSimMs, tSrvMs: tSimMs }, 0)
    },
    events(ev) {
      for (const cb of L.ev) cb(ev.map((e, i) => ({ seq: i + 1, t_sim_ns: 0, type: 'x', level: 0, data: {}, ...e }) as RtEvent))
    },
    playbackState(s) {
      for (const cb of L.pb) cb(s)
    },
    si(p) {
      info = { ...info, ...p } as ServerInfoView
      for (const cb of L.si) cb(info)
    },
  }
  const roster = { size: 1, version: 1, get: () => undefined, idOf: () => undefined, agentNoOf: (id: string) => (id === 'uav1' ? 1 : -1), entries: () => [] }
  S.rt = {
    get serverInfo() {
      return info
    },
    roster,
    onServerInfo: (cb: (s: ServerInfoView) => void) => (L.si.push(cb), () => {}),
    onTime: (cb: (t: TimeFrameView, r: number) => void) => (L.time.push(cb), () => {}),
    onEvents: (cb: (b: readonly RtEvent[]) => void) => (L.ev.push(cb), () => {}),
    onPlaybackState: (cb: (s: PlaybackState) => void) => (L.pb.push(cb), () => {}),
    call(service: string, args: Record<string, unknown>) {
      S.calls.push({ service, args })
      return { id: 'c', result: S.callResult(service), onResult() {}, onProgress() {}, cancel() {} }
    },
    playback(cmd: string, args: Record<string, unknown> = {}) {
      S.pb.push({ cmd, args })
      return S.pbReply(cmd, args)
    },
  } as unknown as RtClient
  return S
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 8; i++) await Promise.resolve()
}

beforeEach(() => {
  vi.stubGlobal('fetch', async () => new Response('{}', { status: 404 }))
  resetTimeline()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('live transport (M12-AC-014, AC-016)', () => {
  it('play sends sim/play, shows pending until TIME confirms', async () => {
    const s = stub()
    bindTimeline(s.rt)
    s.time(TS.PAUSED, 1000)
    tickTimeline(0)
    timeline.play()
    expect(s.calls).toEqual([{ service: 'sim/play', args: {} }])
    expect(timelineStore.getState().pending?.control).toBe('play')
    s.time(TS.PLAYING, 1000)
    expect(timelineStore.getState().pending).toBeNull()
    timeline.togglePlay()
    expect(s.calls[1].service).toBe('sim/pause')
  })

  it('reverts after 1 s without confirmation and explains rejections by reason code', async () => {
    const s = stub()
    bindTimeline(s.rt)
    s.time(TS.PAUSED, 1000)
    tickTimeline(0)
    timeline.play()
    // read the clock after the call: the 1 s deadline starts inside play(), and on a loaded machine more than 1 ms can
    // pass between a reading taken before the call and the call itself (FX-WEB2, full parallel run)
    const t0 = performance.now()
    tickTimeline(t0 + 500)
    expect(timelineStore.getState().pending).not.toBeNull()
    tickTimeline(t0 + 1001)
    expect(timelineStore.getState().pending).toBeNull()
    expect(timelineStore.getState().notice?.key).toBe('timeline.noConfirm')
    s.callResult = async () => ({ id: 'x', status: 'rejected', code: 116 })
    timeline.setRate(5)
    expect(timelineStore.getState().rateRequested).toBe(5)
    await flush()
    expect(timelineStore.getState().notice?.key).toBe('reason.116')
    expect(timelineStore.getState().rateRequested).toBe(1)
    expect(timelineStore.getState().pending).toBeNull()
  })

  it('greys the controls for viewers (no call is sent)', () => {
    const s = stub()
    bindTimeline(s.rt)
    s.si({ role: 'viewer', seat: 'none' })
    s.time(TS.PAUSED, 0)
    tickTimeline(0)
    timeline.play()
    timeline.step('100ms')
    timeline.setRate(2)
    expect(s.calls).toEqual([])
    expect(timelineStore.getState().canWrite).toBe(false)
  })

  it('steps by 25, 250 and 1 ticks only while PAUSED', async () => {
    const s = stub()
    bindTimeline(s.rt)
    s.time(TS.PAUSED, 1000)
    tickTimeline(0)
    timeline.step('100ms')
    await flush()
    timeline.step('1s')
    await flush()
    timeline.step('tick')
    await flush()
    expect(s.calls.map((c) => c.args.ticks)).toEqual([25, 250, 1])
    s.time(TS.PLAYING, 1000)
    tickTimeline(10)
    timeline.step('100ms')
    expect(s.calls.length).toBe(3)
  })

  it('shows the RTF badge when the actual rate stays below 0.95 x requested for 2 s', () => {
    const s = stub()
    bindTimeline(s.rt)
    s.events([{ type: 'sim.clock', data: { rate: 10, state: 'PLAYING' } }])
    s.time(TS.PLAYING, 0, 3.4)
    tickTimeline(0)
    tickTimeline(1000)
    expect(timelineStore.getState().rtfLimited).toBe(false)
    tickTimeline(2100)
    expect(timelineStore.getState().rtfLimited).toBe(true)
    expect(timelineStore.getState().rateActual).toBeCloseTo(3.4, 6)
  })
})

describe('store write rate (M12-AC-020)', () => {
  it('writes at most 4 times per second at the Tier S cadence while playing with events', () => {
    const s = stub()
    bindTimeline(s.rt)
    let writes = 0
    const off = timelineStore.subscribe(() => writes++)
    let tick = 0
    for (let now = 0; now <= 10_000; now += 100) {
      s.time(TS.PLAYING, now)
      if (now % 500 === 0) s.events([{ seq: now + 1, t_sim_ns: now * 1e6, type: 'safety.fence', level: 2 }])
      if (now >= tick) {
        tickTimeline(now)
        tick += 250
      }
    }
    off()
    expect(writes / 10).toBeLessThanOrEqual(4.2)
    expect(timelineStore.getState().tDisplayS).toBeCloseTo(10, 6)
    expect(timelineStore.getState().markersVersion).toBe(timelineTrack.version)
  })
})

describe('replay (FR-025, FR-028, FR-046, FR-047)', () => {
  function openReplay(s: Stub): void {
    s.playbackState({ status: 'paused', run: RUN, segment: 0, dataStart_ns: 0, dataEnd_ns: 600e9, speed_max: 8.77, epoch: 5 })
    s.time(TS.PAUSED | 0x80, 0, 1, 5)
    tickTimeline(0)
  }

  it('seeks optimistically, shows BUFFERING after 100 ms and clears it on did_seek', async () => {
    vi.useFakeTimers()
    const s = stub()
    let answer: (p: PlaybackState) => void = () => {}
    s.pbReply = (cmd) => (cmd === 'seek' ? new Promise((r) => (answer = r)) : Promise.resolve({ status: 'paused' }))
    bindTimeline(s.rt)
    openReplay(s)
    expect(timelineStore.getState().mode).toBe('replay')
    timeline.seek(420)
    await flush()
    expect(s.pb[0]).toMatchObject({ cmd: 'seek', args: { seek_ns: 420e9 } })
    expect(timelineStore.getState().tDisplayS).toBe(420)
    expect(timelineStore.getState().buffering).toBe(false)
    vi.advanceTimersByTime(120)
    expect(timelineStore.getState().buffering).toBe(true)
    // steps while the seek is pending coalesce into one final target
    timeline.step('tick')
    timeline.step('tick')
    expect(s.pb.length).toBe(1)
    answer({ status: 'paused', did_seek: true })
    s.playbackState({ status: 'paused', did_seek: true, current_ns: 420e9 })
    await vi.runAllTimersAsync()
    expect(s.pb.length).toBe(2)
    await flush()
    expect(s.pb[1].args.seek_ns).toBe(Math.round(420.08 * 1e9))
    answer({ status: 'paused', did_seek: true })
    await vi.runAllTimersAsync()
    expect(timelineStore.getState().buffering).toBe(false)
    expect(timelineStore.getState().pending).toBeNull()
  })

  it('maps -> and Shift+-> to +1 s and +10 s seeks, clamps speeds to speed_max and jumps between markers', async () => {
    const s = stub()
    bindTimeline(s.rt)
    openReplay(s)
    s.time(TS.PAUSED | 0x80, 100_000, 1, 6)
    tickTimeline(10)
    timeline.step('100ms')
    await flush()
    expect(s.pb.at(-1)).toMatchObject({ cmd: 'seek', args: { seek_ns: 101e9 } })
    s.pbReply = async (cmd) => (cmd === 'seek' ? { status: 'paused', did_seek: true } : { status: 'paused', speed: 8.77, warnings: ['SPEED_CLAMPED'] })
    timeline.setRate(10) // greyed: above speed_max
    expect(s.pb.at(-1)?.cmd).toBe('seek')
    timeline.setRate(5)
    await flush()
    expect(s.pb.at(-1)).toMatchObject({ cmd: 'speed', args: { speed: 5 } })
    s.pbReply = async () => ({ status: 'paused', did_seek: true })
    timelineTrack.addMarker(250_000, 2, 3, 0xffff, 7)
    s.time(TS.PAUSED | 0x80, 101_000, 1, 7)
    tickTimeline(20)
    timeline.jumpMarker(1)
    await flush()
    expect(s.pb.at(-1)).toMatchObject({ cmd: 'seek', args: { seek_ns: 250e9 } })
  })
})

describe('markers, one red and bookmarks (FR-018, FR-019, FR-026)', () => {
  it('turns live events into markers with one HERO', () => {
    const s = stub()
    bindTimeline(s.rt)
    s.time(TS.PLAYING, 0)
    s.events([
      { seq: 1, t_sim_ns: 1e9, type: 'safety.geofence', level: 3, uav: 'uav1' },
      { seq: 2, t_sim_ns: 2e9, type: 'uav.state', level: 0, data: { to: 'FLYING' } },
      { seq: 3, t_sim_ns: 3e9, type: 'rec.started', level: 1 },
    ])
    expect(timelineTrack.markers.n).toBe(2)
    expect(timelineTrack.markers.agentNo[0]).toBe(1)
    expect(timelineStore.getState().recording).toBe(true)
    tickTimeline(1000)
    expect(timelineTrack.heroIdx).toBe(0)
  })

  it('keeps viewer bookmarks in localStorage per run', async () => {
    const mem = new Map<string, string>()
    vi.stubGlobal('localStorage', { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) })
    const s = stub()
    bindTimeline(s.rt)
    s.si({ role: 'viewer', seat: 'none' })
    s.time(TS.PLAYING, 42_000)
    tickTimeline(0)
    const b = await timeline.addBookmark('front arrives')
    expect(b?.source).toBe('local')
    expect(b?.tS).toBeCloseTo(42, 6)
    const saved = JSON.parse(mem.get(`awr.bm.${RUN}`) ?? '{}') as { items: { t_sim_ns: number; label: string }[] }
    expect(saved.items[0]).toMatchObject({ t_sim_ns: 42e9, label: 'front arrives' })
    await timeline.editBookmark(b!.id, 'renamed')
    expect(timelineStore.getState().bookmarks[0].label).toBe('renamed')
    await timeline.removeBookmark(b!.id)
    expect(timelineStore.getState().bookmarks).toEqual([])
  })
})
