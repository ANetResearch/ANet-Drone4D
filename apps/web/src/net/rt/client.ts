// RtClient (M11-FR-094, §7.5; AWR-17 §6.13; AWR-10 AD-06, §6.5; AWR-14 §7.7). Owner: M11. Main-thread facade over
// rt.worker:
//   * three transferable TelemetryFrame slots; swapFrame() (telemetry phase) copies the ready slot into one persistent
//     FrameFront (stable views, zero allocation besides one view per slot), returns the slot used last frame with the
//     next pull, so the worker acks at the render cadence;
//   * reference-counted subscriptions (the highest rate per topic), merged and sent at most every 250 ms;
//   * calls with a Promise of the final result plus every intermediate result (accepted, running, final) and progress;
//     fleet/cmd/{op} batches (callBatch); teleoperation setpoints (CLIENT_DATA); replay control (playback, D1-ext);
//   * events handed out at most once per frame (AD-03), local event gaps, status banners (cleared on every new
//     connection: the gateway re-sends the active ones), roster and serverInfo views;
//   * the connection state of AWR-14 §7.7 with attempt, next retry, close code, error kind and timestamps; token
//     refresh on 4401/4403/whoami 401 (viewer after 4403); page visibility forwarded to the worker.
// The worker is created with `new Worker(new URL('./rt.worker.ts', import.meta.url))`; where Worker does not exist (Node
// tests) or on request, the same RtHost runs in-process behind a message-passing shim. `?source=fake[&fakeN=200]`
// replaces the WebSocket by FakeSource (D1-AC-35; AWR-18 §8.6 `source=`); `?bench=flight60&scene=full&source=fake`
// without fakeN uses `n` or the 2 vehicles of scenario S1.
import { getToken } from '../api'
import { openFake, type FakeSourceOptions, type FakeWorld } from './FakeSource'
import { FrameFront, SF } from './frame'
import { RtHost, CALL_TIMEOUT_MS, type HostEnv, type SubSpec, type WorkerIn, type WorkerOut, type CtrlMsg } from './session'
import type { SocketLike } from './transport'
import {
  CONN_STATES, str, type BatchOp, type CallHandle, type CallOptions, type CallResult, type ClientStats, type ConnErrorKind, type ConnInfo,
  type ConnState, type DataMsg, type EventGap, type PlaybackArgs, type PlaybackCmd, type PlaybackState, type Progress, type RosterEntry,
  type RosterView, type RtClient, type RtErrorMsg, type RtEvent, type RtInitOptions, type ServerInfoView, type StatusItem, type SubscribeOpts,
  type SwarmSnapshot, type TelemetryFrame, type TimeFrameView,
} from './types'

export * from './types'

export const SUB_FLUSH_MS = 250
/** without a frame loop (no swapFrame for this long) event batches are handed out when they arrive */
export const EVENT_IDLE_MS = 100
export const PLAYBACK_TIMEOUT_MS = 10_000
const SWARM_CAP = 1024
/** the 2 vehicles of scenario S1 used by flight60 `scene=full` (AWR-18 §8.6 (5)) */
const SCENE_FULL_N = 2

export interface WorkerLike {
  postMessage(m: WorkerIn, transfer?: ArrayBuffer[]): void
  onmessage: ((e: { data: WorkerOut }) => void) | null
  terminate(): void
}

export interface RtClientOptions {
  /** run RtHost on the main thread (Node tests, or when Worker is unavailable) */
  inProcess?: boolean
  /** custom host environment for the in-process mode (tests inject clocks and sockets) */
  hostEnv?: Partial<HostEnv>
  /** fresh token after 4401, 4403 or whoami 401 (default: net/api getToken(role, {force: true})) */
  refreshToken?: (role: 'operator' | 'viewer') => Promise<string>
  /** location.search used for the `source=fake` switch (default: globalThis.location?.search) */
  search?: string
}

/** Message-passing shim that runs RtHost in the same thread (asynchronous like a real worker). */
export class LocalPort implements WorkerLike {
  onmessage: ((e: { data: WorkerOut }) => void) | null = null
  readonly host: RtHost
  /** FakeWorlds of this port (one per fake URL, shared by its reconnects) */
  readonly fakeWorlds = new Map<string, FakeWorld>()
  private dead = false
  constructor(env: Partial<HostEnv> = {}) {
    const full: HostEnv = {
      post: (m) => queueMicrotask(() => {
        if (!this.dead) this.onmessage?.({ data: m })
      }),
      socket: (url, protocols) => (url.startsWith('fake:') ? openFake(url, this.fakeWorlds) : (new WebSocket(url, protocols) as unknown as SocketLike)),
      now: () => performance.now(),
      timeOrigin: performance.timeOrigin,
      random: () => 0.5,
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
      ...env,
    }
    this.host = new RtHost(full)
  }
  postMessage(m: WorkerIn): void {
    queueMicrotask(() => {
      if (!this.dead) this.host.onMessage(m)
    })
  }
  terminate(): void {
    this.host.onMessage({ cmd: 'close' })
    this.dead = true
  }
}

/** `?source=fake&fakeN=200&fakeRate=1&fakeFixture=/path.awrrt` -> `fake:?n=...`; otherwise the given url */
export function resolveRtUrl(url: string, search: string): string {
  const q = new URLSearchParams(search)
  if (q.get('source') !== 'fake') return url
  const p = new URLSearchParams()
  const benchN = q.get('bench') === 'flight60' && q.get('scene') === 'full' ? String(SCENE_FULL_N) : null
  p.set('n', q.get('fakeN') ?? q.get('n') ?? benchN ?? '1')
  for (const [k, v] of [['fakeRate', 'rate'], ['fakeWindow', 'window'], ['fakeStart', 'start'], ['fakeFixture', 'fixture'], ['fakeLoop', 'loop'],
    ['fakeGround', 'ground'], ['fakeDrop', 'drop'], ['fakeSeed', 'seed']] as const) {
    const x = q.get(k)
    if (x !== null) p.set(v, x)
  }
  return `fake:?${p.toString()}`
}

/** `fake:?...` URL of FakeSource options (tests, M15 and M06 stories without a backend) */
export function fakeUrl(o: FakeSourceOptions = {}): string {
  const p = new URLSearchParams()
  p.set('n', String(o.n ?? 1))
  if (o.rate !== undefined) p.set('rate', String(o.rate))
  if (o.window !== undefined) p.set('window', String(o.window))
  if (o.start) p.set('start', o.start.join(','))
  if (o.ground) p.set('ground', '1')
  if (o.dropEveryS !== undefined) p.set('drop', String(o.dropEveryS))
  if (o.seed !== undefined) p.set('seed', String(o.seed))
  if (o.eventPeriodS !== undefined) p.set('eventPeriodS', String(o.eventPeriodS))
  if (o.envPeriodS !== undefined) p.set('envPeriodS', String(o.envPeriodS))
  if (o.world) p.set('world', o.world)
  return `fake:?${p.toString()}`
}

interface SubEntry { id: number; topic: string; mode: 'latest' | 'all'; priority?: 0 | 1 | 2 | 3; filter?: SubscribeOpts['filter']; rates: number[]; sent: number }
interface CallEntry { id: string; resolve: (r: CallResult) => void; results: ((r: CallResult) => void)[]; progress: ((p: Progress) => void)[]; last: CallResult | null }

class Roster implements RosterView {
  version = 0
  private list: RosterEntry[] = []
  private readonly byNo = new Map<number, RosterEntry>()
  private readonly byId = new Map<string, RosterEntry>()
  get size(): number {
    return this.list.length
  }
  set(version: number, entries: Record<string, unknown>[]): void {
    this.version = version
    this.list = entries.map((e) => ({
      agentNo: Number(e.agent_no), id: String(e.id), model: str(e.model), kind: str(e.kind, 'uav'),
      producer: str(e.producer), simulated: e.simulated !== false, lifecycle: str(e.lifecycle),
    }))
    this.byNo.clear()
    this.byId.clear()
    for (const e of this.list) {
      this.byNo.set(e.agentNo, e)
      this.byId.set(e.id, e)
    }
  }
  get(agentNo: number): RosterEntry | undefined {
    return this.byNo.get(agentNo)
  }
  idOf(agentNo: number): string | undefined {
    return this.byNo.get(agentNo)?.id
  }
  agentNoOf(id: string): number {
    return this.byId.get(id)?.agentNo ?? -1
  }
  entries(): readonly RosterEntry[] {
    return this.list
  }
}

function serverInfoView(m: CtrlMsg): ServerInfoView {
  const world = (m.world ?? {}) as Record<string, unknown>
  const run = (m.run ?? {}) as Record<string, unknown>
  const clock = (m.clock ?? {}) as Record<string, unknown>
  return {
    sessionId: str(m.sessionId), connId: str(m.connId), worldId: str(world.id),
    contentVersion: str(world.contentVersion), runId: str(run.id), segment: Number(run.segment ?? 0),
    mode: m.mode === 'replay' ? 'replay' : 'live',
    clock: { mode: str(clock.mode, 'lockstep'), pausable: clock.pausable !== false, maxSpeed: Number(clock.max_speed ?? 1), steppable: clock.steppable === true },
    role: (m.role as ServerInfoView['role']) ?? 'viewer', seat: (m.seat as ServerInfoView['seat']) ?? 'none',
    window: Number(m.window ?? 6), layouts: (m.layouts ?? {}) as Record<string, string>,
    principal: str(m.principal), capabilities: Array.isArray(m.capabilities) ? (m.capabilities as unknown[]).map((x) => String(x)) : [],
  }
}

let callCounter = 0
const newCallId = (): string => `c-${Date.now().toString(36)}-${(++callCounter).toString(36)}`

export class RtClientImpl implements RtClient {
  private worker: WorkerLike | null = null
  private state: ConnState = 'IDLE'
  private info: ConnInfo = { attempt: 0, nextInMs: 0, sinceMs: 0 }
  private readonly front = new FrameFront()
  private held: ArrayBuffer | null = null
  private ready: ArrayBuffer | null = null
  private pending = false
  private lastInit: RtInitOptions | null = null
  private readonly subsByTopic = new Map<string, SubEntry>()
  private nextSubId = 1
  private readonly subQueue: SubSpec[] = []
  private readonly unsubQueue: number[] = []
  private subTimer: ReturnType<typeof setTimeout> | null = null
  private lastSubFlush = Number.NEGATIVE_INFINITY
  private readonly calls = new Map<string, CallEntry>()
  private readonly statusItems = new Map<string, StatusItem>()
  private readonly lEvents = new Set<(b: readonly RtEvent[]) => void>()
  private readonly lGap = new Set<(g: EventGap) => void>()
  private readonly lStatus = new Set<(i: readonly StatusItem[]) => void>()
  private readonly lConn = new Set<(s: ConnState, i: ConnInfo) => void>()
  private readonly lTime = new Set<(t: TimeFrameView, recvMs: number) => void>()
  private readonly lData = new Set<(m: DataMsg) => void>()
  private readonly lInfo = new Set<(s: ServerInfoView) => void>()
  private readonly lPlayback = new Set<(s: PlaybackState) => void>()
  private readonly lError = new Set<(e: RtErrorMsg) => void>()
  private readonly playbackWaiters = new Map<string, { resolve: (s: PlaybackState) => void; timer: ReturnType<typeof setTimeout> }>()
  private readonly timeView: TimeFrameView = { state: 0, epoch: 0, rate: 0, tSimMs: 0, tSrvMs: 0 }
  private evPending: RtEvent[] = []
  private evTimer: ReturnType<typeof setTimeout> | null = null
  private lastSwapAt = Number.NEGATIVE_INFINITY
  private swapWinStart = 0
  private swapCount = 0
  private hidden = false
  private visHandler: (() => void) | null = null
  private pbCounter = 0
  readonly roster = new Roster()
  serverInfo: ServerInfoView | null = null
  readonly swarm: SwarmSnapshot = {
    n: 0, seq: 0, tSimMs: 0, version: 0, agentNo: this.front.swarm.agentNo, fs: this.front.swarm.fs, battery: this.front.swarm.battery,
    flags: this.front.swarm.flags, ctrl: this.front.swarm.ctrl, pos: this.front.swarm.pos, vel: this.front.swarm.vel, quat: this.front.swarm.quat,
  }
  /** counters for tests and __perf.net */
  readonly counters = { swaps: 0, slots: 0, ctrlOnly: 0, eventBatches: 0, defects: 0, localGaps: 0 }

  constructor(private readonly opts: RtClientOptions = {}) {}

  get status(): ConnState {
    return this.state
  }
  get connInfo(): ConnInfo {
    return this.info
  }

  init(o: RtInitOptions): void {
    if (this.worker) return
    this.lastInit = o
    const search = this.opts.search ?? (typeof location !== 'undefined' ? location.search : '')
    const url = resolveRtUrl(o.url, search)
    const inProcess = this.opts.inProcess || typeof Worker === 'undefined'
    const w: WorkerLike = inProcess
      ? new LocalPort(this.opts.hostEnv)
      : (new Worker(new URL('./rt.worker.ts', import.meta.url), { type: 'module', name: 'rt' }) as unknown as WorkerLike)
    this.worker = w
    w.onmessage = (e) => this.onWorker(e.data)
    const slots = [new ArrayBuffer(this.front.buf.byteLength), new ArrayBuffer(this.front.buf.byteLength), new ArrayBuffer(this.front.buf.byteLength)]
    this.held = null
    this.ready = null
    this.pending = false
    w.postMessage({ cmd: 'init', url, token: o.token, tier: o.tier, deviceClass: o.deviceClass, timeOriginMain: performance.timeOrigin, slots }, slots)
    // re-send the live subscriptions (a close()/init() pair keeps the component subscriptions)
    const again: SubSpec[] = []
    this.posted.clear()
    this.subQueue.length = 0
    this.unsubQueue.length = 0
    for (const e of this.subsByTopic.values()) {
      e.sent = Math.max(...e.rates)
      again.push(this.specOf(e))
      this.posted.add(e.id)
    }
    if (again.length) w.postMessage({ cmd: 'sub', subs: again })
    if (this.hidden) w.postMessage({ cmd: 'visibility', hidden: true })
    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function' && !this.visHandler) {
      const h = (): void => this.setHidden(document.visibilityState === 'hidden')
      this.visHandler = h
      document.addEventListener('visibilitychange', h)
      h()
    }
    this.setState('CONNECTING', { attempt: 0, nextInMs: 0 })
  }

  close(): void {
    if (!this.worker) return
    this.worker.postMessage({ cmd: 'close' })
    this.worker.terminate()
    this.worker = null
    if (this.subTimer) clearTimeout(this.subTimer)
    this.subTimer = null
    if (this.evTimer) clearTimeout(this.evTimer)
    this.evTimer = null
    if (this.visHandler && typeof document !== 'undefined') document.removeEventListener('visibilitychange', this.visHandler)
    this.visHandler = null
    this.subQueue.length = 0
    this.unsubQueue.length = 0
    this.posted.clear()
    this.held = null
    this.ready = null
    this.pending = false
    this.evPending = []
    for (const c of this.calls.values()) c.resolve({ id: c.id, status: 'canceled', code: 6, reason: 'CANCELLED', message: 'client closed', final: true })
    this.calls.clear()
    for (const [id, p] of this.playbackWaiters) {
      clearTimeout(p.timer)
      p.resolve({ status: 'error', code: 213, request_id: id })
    }
    this.playbackWaiters.clear()
    this.setState('CLOSED', { attempt: 0, nextInMs: 0 })
  }

  reconnectNow(): void {
    this.worker?.postMessage({ cmd: 'reconnect' })
  }

  reauthenticate(token: string, role: 'operator' | 'viewer'): void {
    this.worker?.postMessage({ cmd: 'reauth', token, role })
  }

  setHidden(hidden: boolean): void {
    if (hidden === this.hidden) return
    this.hidden = hidden
    this.worker?.postMessage({ cmd: 'visibility', hidden })
  }

  setClientStats(s: ClientStats): void {
    this.worker?.postMessage({ cmd: 'stats', stats: { ...s } })
  }

  // ------------------------------------------------------------ frames
  swapFrame(): TelemetryFrame | null {
    const w = this.worker
    if (!w) return null
    const now = performance.now()
    this.lastSwapAt = now
    // frame rate of the telemetry phase, reported to the worker once per second (ack.fps, clientStats.fps)
    this.swapCount++
    if (now - this.swapWinStart >= 1000) {
      if (this.swapWinStart > 0) w.postMessage({ cmd: 'stats', stats: { fps: Math.round((this.swapCount * 1e4) / (now - this.swapWinStart)) / 10 } })
      this.swapWinStart = now
      this.swapCount = 0
    }
    if (this.evPending.length) this.deliverEvents()
    const slot = this.ready
    if (slot) {
      this.ready = null
      this.front.load(slot)
      const returned = this.held
      this.held = slot // kept until next frame: the worker acks what the renderer has used (AWR-17 §6.9 L1)
      this.pending = true
      w.postMessage({ cmd: 'pull', returned }, returned ? [returned] : [])
      this.counters.swaps++
      this.onSlot(this.front)
      return this.front
    }
    if (!this.pending) {
      this.pending = true
      w.postMessage({ cmd: 'pull', returned: null })
    }
    return null
  }

  private onSlot(s: FrameFront): void {
    const h = s.hdr
    if (h.flags & SF.TIME_CHANGED) {
      const t = this.timeView
      t.state = h.timeState
      t.epoch = h.timeEpoch
      t.rate = h.timeRate
      t.tSimMs = h.timeTSimMs
      t.tSrvMs = h.timeTSrvMs
      for (const cb of this.lTime) cb(t, h.timeRecvMainMs)
    }
    if (h.swarmN > 0) {
      // the arrays are views over the front image, which now holds this sample: only the bookkeeping changes
      const sw = this.swarm
      sw.n = Math.min(h.swarmN, SWARM_CAP)
      sw.seq = h.swarmSeq
      sw.tSimMs = h.swarmTSimMs
      sw.version++
    }
    const cs = CONN_STATES[h.connState]
    if (cs && cs !== this.state && (h.flags & SF.CONN_CHANGED) === 0 && this.state !== 'CLOSED') this.setState(cs, this.info)
  }

  private onWorker(out: WorkerOut): void {
    if (out.slot) {
      this.ready = out.slot
      this.pending = false
      this.counters.slots++
    } else this.counters.ctrlOnly++
    if (out.ctrl) this.onCtrl(out.ctrl)
  }

  private onCtrl(list: CtrlMsg[]): void {
    let statusChanged = false
    for (const m of list) {
      switch (m.op) {
        case 'conn':
          this.setState(m.state as ConnState, { attempt: Number(m.attempt) || 0, nextInMs: Number(m.nextInMs) || 0, code: Number(m.code) || undefined,
            error: (m.error as ConnErrorKind | undefined) ?? undefined })
          break
        case 'connected':
          // a new connection: the gateway re-sends every active status after serverInfo
          if (this.statusItems.size) {
            this.statusItems.clear()
            statusChanged = true
          }
          break
        case 'serverInfo': {
          this.serverInfo = serverInfoView(m)
          const si = this.serverInfo
          for (const cb of this.lInfo) cb(si)
          break
        }
        case 'roster':
          this.roster.set(Number(m.version) || 0, (m.entries as Record<string, unknown>[]) ?? [])
          break
        case 'data': {
          const d: DataMsg = { topic: String(m.topic), channelId: Number(m.channelId), seq: Number(m.seq), tSimMs: Number(m.tSimMs), data: m.data }
          for (const cb of this.lData) cb(d)
          break
        }
        case 'result':
          this.onResult(m as unknown as CallResult)
          break
        case 'progress': {
          const c = this.calls.get(String(m.id))
          if (c) for (const cb of c.progress) cb(m as unknown as Progress)
          break
        }
        case 'event': {
          const { op: _op, ...ev } = m
          this.evPending.push(ev as unknown as RtEvent)
          break
        }
        case 'events':
          for (const it of (m.items as RtEvent[]) ?? []) this.evPending.push(it)
          break
        case 'status':
          this.statusItems.set(String(m.id), m as unknown as StatusItem)
          statusChanged = true
          break
        case 'removeStatus':
          for (const id of (m.ids as string[]) ?? []) this.statusItems.delete(id)
          statusChanged = true
          break
        case 'needToken':
          this.refreshToken(m.role === 'viewer' ? 'viewer' : 'operator')
          break
        case 'localGap': {
          this.counters.localGaps++
          const g: EventGap = { fromSeq: Number(m.fromSeq) || 0, toSeq: Number(m.toSeq) || 0, dropped: Number(m.dropped) || 0 }
          for (const cb of this.lGap) cb(g)
          break
        }
        case 'playbackState': {
          const { op: _op, ...ps } = m
          const st = ps as unknown as PlaybackState
          const rid = st.request_id
          const waiter = rid ? this.playbackWaiters.get(rid) : undefined
          if (waiter && rid) {
            clearTimeout(waiter.timer)
            this.playbackWaiters.delete(rid)
            waiter.resolve(st)
          }
          for (const cb of this.lPlayback) cb(st)
          break
        }
        case 'error': {
          const e: RtErrorMsg = { code: Number(m.code) || 0, name: str(m.name), message: str(m.message), ref: m.ref as RtErrorMsg['ref'] }
          for (const cb of this.lError) cb(e)
          break
        }
        case 'defect':
          this.counters.defects++
          console.warn(`awr rt: ${str(m.message)}`)
          break
        default:
          break
      }
    }
    if (statusChanged) {
      const items = [...this.statusItems.values()]
      for (const cb of this.lStatus) cb(items)
    }
    if (this.evPending.length) {
      if (performance.now() - this.lastSwapAt > EVENT_IDLE_MS) this.deliverEvents()
      else if (!this.evTimer) {
        // a frame loop is running: the next swapFrame hands the batch out; the timer covers a loop that just stopped
        this.evTimer = setTimeout(() => {
          this.evTimer = null
          if (this.evPending.length) this.deliverEvents()
        }, EVENT_IDLE_MS)
      }
    }
  }

  private deliverEvents(): void {
    const batch = this.evPending
    this.evPending = []
    if (this.evTimer) {
      clearTimeout(this.evTimer)
      this.evTimer = null
    }
    this.counters.eventBatches++
    for (const cb of this.lEvents) cb(batch)
  }

  private refreshToken(role: 'operator' | 'viewer'): void {
    const f = this.opts.refreshToken ?? ((r: 'operator' | 'viewer') => getToken(r, { force: true }))
    f(role).then(
      (token) => this.worker?.postMessage({ cmd: 'token', token }),
      () => this.worker?.postMessage({ cmd: 'token', token: '' }),
    )
  }

  private setState(s: ConnState, info: ConnInfo): void {
    const now = typeof performance !== 'undefined' ? performance.now() : 0
    const changed = s !== this.state || info.code !== this.info.code || info.error !== this.info.error
    const wasLive = this.state === 'LIVE' || this.state === 'DEGRADED'
    const next: ConnInfo = { ...info, sinceMs: s !== this.state ? now : (this.info.sinceMs ?? now), lastLiveMs: this.info.lastLiveMs }
    if (wasLive && s !== 'LIVE' && s !== 'DEGRADED') next.lastLiveMs = now
    this.state = s
    this.info = next
    if (changed) for (const cb of this.lConn) cb(s, next)
  }

  // ------------------------------------------------------------ subscriptions
  subscribe(topic: string, o: SubscribeOpts): () => void {
    let e = this.subsByTopic.get(topic)
    const rate = o.rate
    if (!e) {
      e = { id: this.nextSubId++, topic, mode: o.mode ?? 'latest', priority: o.priority, filter: o.filter, rates: [], sent: -1 }
      this.subsByTopic.set(topic, e)
    }
    const entry = e
    entry.rates.push(rate)
    this.syncSub(entry)
    let released = false
    return () => {
      if (released) return
      released = true
      const i = entry.rates.indexOf(rate)
      if (i >= 0) entry.rates.splice(i, 1)
      if (entry.rates.length === 0) {
        this.subsByTopic.delete(topic)
        if (this.posted.has(entry.id)) this.queueUnsub(entry.id)
        else {
          // never reached the worker: drop the queued subscribe
          const k = this.subQueue.findIndex((s) => s.id === entry.id)
          if (k >= 0) this.subQueue.splice(k, 1)
        }
      } else this.syncSub(entry)
    }
  }

  /** subscription ids posted to the current worker */
  private readonly posted = new Set<number>()

  private specOf(e: SubEntry): SubSpec {
    const s: SubSpec = { id: e.id, topic: e.topic, rate: e.sent, mode: e.mode }
    if (e.priority !== undefined) s.priority = e.priority
    if (e.filter) s.filter = e.filter
    return s
  }

  private syncSub(e: SubEntry): void {
    const max = Math.max(...e.rates)
    if (max === e.sent) return
    e.sent = max
    const k = this.subQueue.findIndex((s) => s.id === e.id)
    if (k >= 0) this.subQueue.splice(k, 1)
    this.subQueue.push(this.specOf(e))
    this.scheduleSubs()
  }
  private queueUnsub(id: number): void {
    const k = this.subQueue.findIndex((s) => s.id === id)
    if (k >= 0) this.subQueue.splice(k, 1)
    this.unsubQueue.push(id)
    this.scheduleSubs()
  }
  private scheduleSubs(): void {
    if (this.subTimer) return
    const wait = Math.max(0, this.lastSubFlush + SUB_FLUSH_MS - performance.now())
    this.subTimer = setTimeout(() => this.flushSubs(), wait)
  }
  private flushSubs(): void {
    this.subTimer = null
    this.lastSubFlush = performance.now()
    const w = this.worker
    if (!w) return // sent by init()
    if (this.subQueue.length) {
      const subs = this.subQueue.splice(0)
      for (const s of subs) this.posted.add(s.id)
      w.postMessage({ cmd: 'sub', subs })
    }
    if (this.unsubQueue.length) {
      const ids = this.unsubQueue.splice(0)
      for (const id of ids) this.posted.delete(id)
      w.postMessage({ cmd: 'unsub', ids })
    }
  }

  // ------------------------------------------------------------ calls
  call(service: string, args: object, o: CallOptions = {}): CallHandle {
    const id = o.id ?? newCallId()
    let resolve!: (r: CallResult) => void
    const result = new Promise<CallResult>((r) => {
      resolve = r
    })
    const entry: CallEntry = { id, resolve, results: [], progress: [], last: null }
    const handle: CallHandle = {
      id,
      result,
      onResult: (cb) => {
        entry.results.push(cb)
        if (entry.last) cb(entry.last)
      },
      onProgress: (cb) => {
        entry.progress.push(cb)
      },
      cancel: () => this.worker?.postMessage({ cmd: 'cancel', id }),
    }
    if (!this.worker) {
      const r: CallResult = { id, status: 'rejected', code: 213, reason: 'SERVICE_UNAVAILABLE', message: 'realtime client not initialised', final: true }
      entry.last = r
      resolve(r)
      return handle
    }
    this.calls.set(id, entry)
    this.worker.postMessage({ cmd: 'call', id, service, args, timeoutMs: o.timeoutMs ?? CALL_TIMEOUT_MS, confirm: o.confirm })
    return handle
  }

  callBatch(op: BatchOp, vehicles: readonly string[] | '*', args?: object, o?: CallOptions): CallHandle {
    const body: Record<string, unknown> = { vehicles: vehicles === '*' ? '*' : [...vehicles] }
    if (args) body.args = args
    return this.call(`fleet/cmd/${op}`, body, o)
  }

  private onResult(r: CallResult): void {
    const c = this.calls.get(r.id)
    if (!c) return
    c.last = r
    for (const cb of c.results) cb(r)
    if (r.final) {
      this.calls.delete(r.id)
      c.resolve(r)
    }
  }

  publishSetpoint(agentNo: number, vx: number, vy: number, vz: number, yawRate: number, final = false): void {
    const id = this.roster.idOf(agentNo)
    if (!id || !this.worker) return
    this.worker.postMessage({ cmd: 'setpoint', agentNo, id, vx, vy, vz, yawRate, final })
  }

  playback(cmd: PlaybackCmd, args: PlaybackArgs = {}): Promise<PlaybackState> {
    const rid = args.request_id ?? `pb-${Date.now().toString(36)}-${(++this.pbCounter).toString(36)}`
    const w = this.worker
    if (!w) return Promise.resolve({ status: 'error', code: 213, request_id: rid })
    return new Promise<PlaybackState>((resolve) => {
      const timer = setTimeout(() => {
        this.playbackWaiters.delete(rid)
        resolve({ status: 'error', code: 213, request_id: rid })
      }, PLAYBACK_TIMEOUT_MS)
      this.playbackWaiters.set(rid, { resolve, timer })
      const msg: Record<string, unknown> = { cmd, request_id: rid }
      if (args.run !== undefined) msg.run = args.run
      if (args.segment !== undefined) msg.segment = args.segment
      if (args.seek_ns !== undefined) msg.seek_ns = Math.round(args.seek_ns)
      if (args.speed !== undefined) msg.speed = args.speed
      w.postMessage({ cmd: 'playback', msg })
    })
  }

  // ------------------------------------------------------------ listeners
  onEvents(cb: (b: readonly RtEvent[]) => void): () => void {
    return add(this.lEvents, cb)
  }
  onEventGap(cb: (g: EventGap) => void): () => void {
    return add(this.lGap, cb)
  }
  onStatus(cb: (i: readonly StatusItem[]) => void): () => void {
    return add(this.lStatus, cb)
  }
  onConnState(cb: (s: ConnState, i: ConnInfo) => void): () => void {
    return add(this.lConn, cb)
  }
  onTime(cb: (t: TimeFrameView, recvMs: number) => void): () => void {
    return add(this.lTime, cb)
  }
  onData(cb: (m: DataMsg) => void): () => void {
    return add(this.lData, cb)
  }
  onServerInfo(cb: (s: ServerInfoView) => void): () => void {
    return add(this.lInfo, cb)
  }
  onPlaybackState(cb: (s: PlaybackState) => void): () => void {
    return add(this.lPlayback, cb)
  }
  /** `error` ops for non-call requests (subscribe 314/315, CLIENT_DATA 322, ...) */
  onError(cb: (e: RtErrorMsg) => void): () => void {
    return add(this.lError, cb)
  }
  /** the current status banners */
  statusList(): readonly StatusItem[] {
    return [...this.statusItems.values()]
  }
  /** the options of the last init() (the viewport reads the tier from here) */
  get initOptions(): RtInitOptions | null {
    return this.lastInit
  }
  /** the in-process host (inProcess mode only; tests) */
  get localHost(): RtHost | null {
    return this.worker instanceof LocalPort ? this.worker.host : null
  }
  /** the FakeWorld behind a fake URL (inProcess mode only; tests and stories) */
  fakeWorld(): FakeWorld | null {
    return this.worker instanceof LocalPort ? ([...this.worker.fakeWorlds.values()][0] ?? null) : null
  }
}

function add<T>(set: Set<T>, cb: T): () => void {
  set.add(cb)
  return () => {
    set.delete(cb)
  }
}

// ------------------------------------------------------------ page instance
// One RtClient per page (AWR-10 §6.5): createRtClient() without options returns the page instance, so the M15
// RtProvider (StrictMode may call its initialiser twice), the telemetry phase and the GoTo path share one worker.
let shared: RtClientImpl | null = null
export function createRtClient(o?: RtClientOptions): RtClientImpl {
  if (o) return new RtClientImpl(o)
  shared ??= new RtClientImpl()
  return shared
}
/** the page instance, or null before anything created it */
export function rtClient(): RtClientImpl | null {
  return shared
}

/**
 * An RtClient backed by FakeSource in-process (M11-FR-098 "same interface as RtClient"): initialised and connected, for
 * unit tests and UI stories without a backend.
 */
export function createFakeRtClient(o: FakeSourceOptions & { tier?: 'A' | 'B' | 'S' } = {}): RtClientImpl {
  const c = new RtClientImpl({ inProcess: true, search: '', refreshToken: () => Promise.resolve('') })
  c.init({ url: fakeUrl(o), token: '', tier: o.tier ?? 'S', deviceClass: 'software' })
  return c
}
