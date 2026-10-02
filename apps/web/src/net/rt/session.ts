// rt.worker protocol engine (M11 §6.4.14, §6.6.3; AWR-17 §6.2-§6.13; AWR-14 §7.6, §7.7). Owner: M11.
// Pure TypeScript without DOM worker globals: rt.worker.ts binds it to `self`, tests and the in-process client drive it
// directly. Responsibilities:
//   * the only WebSocket (or FakeSource); handshake with the layout-hash and contracts-major check (1000 self-close and
//     FATAL `version`), hello with resume, subscriptions replayed and in-flight calls re-sent with the same id;
//   * reconnect with backoff and the close-code table of AWR-17 §8.3 / AWR-14 §7.7: 1006 triaged by whoami (401 refresh,
//     403 FATAL `auth`), 4401 fresh token and immediate reconnect, 4403 viewer token (E-16), 4429 30 s (E-17), 1013
//     (E-09), 1009 (defect), 1002 x3 / 1008 / 4426 FATAL;
//   * TIME handled on arrival (epoch switch clears the references; a new-epoch BATCH waits one TIME, old-epoch BATCH are
//     dropped); BATCH records indexed by reference only (decode.ts); slots composed on pull into the persistent image and
//     copied into the free transferable slot (frame.ts); ack when the slot is handed to the main thread's pull (3 frames or
//     34 ms, with fps, decodeMs, lagMs; ADR-067); ClockSync pings with srttMs; DEGRADED after 1 s without TIME; clientStats
//     at 1 Hz;
//   * control messages batched per frame; while the page is hidden events stay in the worker (cap 8192, local gap);
//   * CLIENT_DATA setpoints (per-vehicle client channels, one preallocated 32 B frame) and playback pass-through.
import { Reason, REASONS, WS_CLOSE } from '@awr/contracts/reasons'
import {
  BATCH_SNAPSHOT, CD_FINAL, CONTRACTS_VERSION, OP_BATCH, OP_TIME, SCHEMA_HASH, TimeState, newRecordView, newRtBatchHeader,
  newTimeView, readFrameHeader, readTime, writeClientData,
} from './layouts'
import { ClockSync } from './clockSync'
import { CtrlQueue } from './ctrl'
import { Decoder, type CtrlMsg } from './decode'
import { H, RESET_MAX, SF, SLOT_MAGIC, SlotWriter } from './frame'
import { BACKOFF, WS_OPEN, backoffDelay, protocolsFor, type SocketFactory, type SocketLike } from './transport'
import { CONN_STATES, str, type ClientStats, type ConnErrorKind, type ConnState } from './types'

export type { CtrlMsg } from './decode'
export { EVENT_BUFFER_CAP } from './ctrl'

export const CLIENT_NAME = 'awr-web/0.1.0'
/** schemas this client decodes with generated accessors (hash checked against serverInfo.layouts, M11-FR-100) */
export const DECODED_SCHEMAS = ['awr.SwarmLite32.v1', 'awr.DroneState64.v1', 'awr.EnvSample32.v1', 'awr.SensorPose48.v1',
  'awr.rt.BatchHeader.v1', 'awr.rt.RecordHeader.v1', 'awr.rt.Time.v1', 'awr.rt.ClientDataHeader.v1', 'awr.VelSetpoint16.v1'] as const
export const ACK_EVERY_FRAMES = 3
/** ack coalescing: every 3 frames or when the last ack is this old (two 60 Hz ticks; one ack per pull below 30 fps, ADR-067) */
export const ACK_MAX_GAP_MS = 34
export const STALE_AFTER_MS = 1000
export const CALL_TIMEOUT_MS = 3000
export const CTRL_FLUSH_MS = 50
/** control delivery period while the page is hidden (events stay in the worker) */
export const CTRL_HIDDEN_MS = 250
export const CLIENT_STATS_MS = 1000
/** consecutive auth failures (4401, whoami 401) before FATAL `auth` (E-08) */
export const AUTH_RETRY_MAX = 2
/** the main thread normally answers needToken at once; after this the host reconnects with the token it has */
export const TOKEN_WAIT_MS = 5000
/** client-publish channel ids (AWR-17 §6.4 CLIENT_DATA: 1-255) */
export const CLIENT_CH_MAX = 255

// connection state codes (index in CONN_STATES)
const C_IDLE = 0, C_CONNECTING = 1, C_SYNCING = 2, C_LIVE = 3, C_DEGRADED = 4, C_RECONNECTING = 5, C_FATAL = 6, C_CLOSED = 7

export interface SubSpec { id: number; topic: string; rate: number; mode: 'latest' | 'all'; priority?: number; filter?: { types?: readonly string[]; levelMin?: number } }
export type WorkerIn =
  | { cmd: 'init'; url: string; token: string; tier: 'A' | 'B' | 'S'; deviceClass: string; timeOriginMain: number; slots: ArrayBuffer[] }
  | { cmd: 'pull'; returned: ArrayBuffer | null }
  | { cmd: 'sub'; subs: SubSpec[] }
  | { cmd: 'unsub'; ids: number[] }
  | { cmd: 'call'; id: string; service: string; args: object; timeoutMs: number; confirm?: string }
  | { cmd: 'cancel'; id: string }
  | { cmd: 'token'; token: string }
  | { cmd: 'reconnect' }
  | { cmd: 'close' }
  | { cmd: 'setpoint'; agentNo: number; id: string; vx: number; vy: number; vz: number; yawRate: number; final: boolean }
  | { cmd: 'playback'; msg: Record<string, unknown> }
  | { cmd: 'stats'; stats: ClientStats }
  | { cmd: 'visibility'; hidden: boolean }
  | { cmd: 'reauth'; token: string; role: 'operator' | 'viewer' }
export interface WorkerOut { slot: ArrayBuffer | null; ctrl: CtrlMsg[] | null }

export interface HostEnv {
  post(msg: WorkerOut, transfer: ArrayBuffer[]): void
  socket: SocketFactory
  now(): number
  timeOrigin: number
  random(): number
  setTimeout(fn: () => void, ms: number): unknown
  clearTimeout(h: unknown): void
  /** HTTP status of GET /api/auth/whoami with the token (1006 triage, AWR-17 §3.3 item 4); omitted: no triage */
  checkAuth?(wsUrl: string, token: string): Promise<number>
}

interface CallSpec { id: string; service: string; args: object; timeoutMs: number; confirm?: string; timer: unknown; answered: boolean; cancel: boolean }

const round3 = (x: number): number => Math.round(x * 1000) / 1000

export class RtHost {
  private sock: SocketLike | null = null
  private conn = C_IDLE
  private attempt = 0
  private reconnects = 0
  private proto1002 = 0
  private authFails = 0
  private url = ''
  private token = ''
  private role: 'operator' | 'viewer' = 'operator'
  private tier = 'S'
  private deviceClass = 'software'
  private originMain = 0
  private closedByUs = false
  private waitingToken = false
  private tConnect: unknown = null
  private tRetry: unknown = null
  private tPing: unknown = null
  private tStale: unknown = null
  private tCtrl: unknown = null
  private tStats: unknown = null
  private tToken: unknown = null
  private helloSent = false
  private sessionId = ''
  private lastCloseCode = 0
  private lastError: ConnErrorKind | undefined
  readonly clock = new ClockSync()
  readonly dec = new Decoder()
  private readonly writer = new SlotWriter()
  // frame state
  private curEpoch = -1
  private stash: ArrayBuffer | null = null
  private readonly time = newTimeView()
  private timeRecvAt = Number.NEGATIVE_INFINITY
  private readonly fh = newRtBatchHeader()
  private readonly rv = newRecordView()
  // pull, ack and slot FIFO (returned slots come back in send order: the frameSeqMax of each is remembered here)
  private readonly free: ArrayBuffer[] = []
  private pullPending = false
  private consumed = 0
  private lastAck = 0
  private lastAckAt = 0
  private decodeMs = 0
  private lastAgeMs = Number.NaN
  private readonly sentSeq = new Float64Array(8)
  private sentHead = 0
  private sentN = 0
  private winPulls = 0
  private pullWinStart = 0
  private pullFps = 0
  // control path
  private readonly ctrl = new CtrlQueue()
  private hidden = false
  private readonly subs = new Map<number, SubSpec>()
  private readonly calls = new Map<string, CallSpec>()
  private lastEventSeq = 0
  private readonly sink = (m: CtrlMsg): void => this.ctrl.push(m)
  // client publish (CLIENT_DATA)
  private readonly setpointCh = new Map<number, { ch: number; seq: number }>()
  private nextClientCh = 1
  private readonly cdBuf = new ArrayBuffer(32)
  private readonly cdView = new DataView(this.cdBuf)
  private mainStats: ClientStats = {}

  constructor(private readonly env: HostEnv) {}

  get connState(): ConnState {
    return CONN_STATES[this.conn]
  }
  get socket(): SocketLike | null {
    return this.sock
  }
  /** test and diagnostics view */
  get counters(): { reconnects: number; malformed: number; droppedEpochFrames: number; eventGaps: number; consumed: number; lastAck: number } {
    return { reconnects: this.reconnects, malformed: this.dec.malformedFrames, droppedEpochFrames: this.dec.droppedEpochFrames,
      eventGaps: this.dec.eventGaps, consumed: this.consumed, lastAck: this.lastAck }
  }
  get currentError(): ConnErrorKind | undefined {
    return this.lastError
  }

  // ------------------------------------------------------------ inbound commands (main thread)
  onMessage(m: WorkerIn): void {
    switch (m.cmd) {
      case 'init':
        this.url = m.url
        this.token = m.token
        this.tier = m.tier
        this.deviceClass = m.deviceClass
        this.originMain = m.timeOriginMain
        for (const b of m.slots) this.free.push(b)
        this.closedByUs = false
        this.connect()
        break
      case 'pull':
        this.onPull(m.returned)
        break
      case 'sub': {
        const fresh: SubSpec[] = []
        for (const s of m.subs) {
          this.subs.set(s.id, s)
          fresh.push(s)
        }
        if (this.helloSent && fresh.length) this.sendJson({ op: 'subscribe', subs: fresh.map(subWire) })
        break
      }
      case 'unsub': {
        const ids = m.ids.filter((id) => this.subs.delete(id))
        if (ids.length) this.dec.ch.onUnsubscribed(ids)
        if (this.helloSent && ids.length) this.sendJson({ op: 'unsubscribe', ids })
        break
      }
      case 'call':
        this.call(m)
        break
      case 'cancel': {
        const c = this.calls.get(m.id)
        if (this.helloSent) this.sendJson({ op: 'cancel', id: m.id })
        else if (c) c.cancel = true // sent after the call is re-sent on reconnect
        break
      }
      case 'token':
        this.token = m.token
        if (this.waitingToken) {
          this.waitingToken = false
          this.clearTimer('tToken')
          this.connect()
        }
        break
      case 'reconnect':
        if (this.conn === C_RECONNECTING || this.conn === C_FATAL) {
          this.attempt = 0
          this.proto1002 = 0
          this.authFails = 0
          this.waitingToken = false
          this.connect()
        }
        break
      case 'close':
        this.closedByUs = true
        this.clearTimers()
        this.clearTimer('tCtrl')
        this.setConn(C_CLOSED)
        this.sock?.close(WS_CLOSE.NORMAL)
        this.sock = null
        break
      case 'setpoint':
        this.setpoint(m)
        break
      case 'playback':
        this.sendJson({ ...m.msg, op: 'playback' })
        break
      case 'stats':
        this.mainStats = { ...this.mainStats, ...m.stats }
        break
      case 'reauth':
        // a new token (seat acquired, or back to viewer): reconnect at once with it, no backoff
        this.token = m.token
        this.role = m.role
        this.waitingToken = false
        this.attempt = 0
        this.authFails = 0
        if (!this.closedByUs) {
          this.abortSilently()
          this.connect()
        }
        break
      case 'visibility':
        this.hidden = m.hidden
        if (this.ctrl.length || this.ctrl.dropped) {
          this.clearTimer('tCtrl')
          this.scheduleCtrl()
        }
        break
    }
  }

  // ------------------------------------------------------------ connection
  private connect(): void {
    this.clearTimers()
    this.helloSent = false
    this.stash = null
    this.setpointCh.clear()
    this.nextClientCh = 1
    this.dec.ch.resetSubscriptions()
    this.setConn(C_CONNECTING, { error: this.lastError })
    let s: SocketLike
    try {
      s = this.env.socket(this.url, protocolsFor(this.token))
    } catch {
      this.onClose(WS_CLOSE.ABNORMAL)
      return
    }
    this.sock = s
    s.binaryType = 'arraybuffer'
    this.tConnect = this.env.setTimeout(() => {
      this.tConnect = null
      if (this.sock === s && s.readyState !== WS_OPEN) this.abortAttempt(WS_CLOSE.ABNORMAL)
    }, BACKOFF.connectTimeoutMs)
    s.onopen = () => {
      if (this.sock === s) this.clearTimer('tConnect')
    }
    s.onmessage = (e) => {
      if (this.sock === s) this.onSocketMessage(e.data)
    }
    s.onclose = (e) => {
      if (this.sock === s) this.onClose(e.code)
    }
    s.onerror = () => {}
  }

  /** drop the current socket without waiting for its close event */
  private abortAttempt(code: number): void {
    const s = this.sock
    if (s) {
      s.onclose = null
      s.onmessage = null
      try {
        s.close()
      } catch {
        // already closed
      }
    }
    this.onClose(code)
  }

  private onClose(code: number): void {
    this.sock = null
    this.helloSent = false
    this.lastCloseCode = code
    this.clearTimers()
    if (this.closedByUs) {
      this.setConn(C_CLOSED)
      return
    }
    if (this.conn === C_FATAL) return // our own 1000 after a version or layout mismatch
    switch (code) {
      case WS_CLOSE.POLICY_VIOLATION:
        this.fatal(code, 'policy')
        return
      case WS_CLOSE.VERSION_MISMATCH:
        this.fatal(code, 'version')
        return
      case WS_CLOSE.PROTOCOL_ERROR:
        if (++this.proto1002 >= 3) {
          this.fatal(code, 'protocol')
          return
        }
        break
      case WS_CLOSE.AUTH:
        this.needToken(code, undefined)
        return
      case WS_CLOSE.SEAT_REVOKED:
        this.role = 'viewer' // E-16: control taken over, continue read-only
        this.needToken(code, 'seat_revoked')
        return
      case WS_CLOSE.CONN_LIMIT:
        this.retry(code, BACKOFF.connLimitMs, 'conn_limit')
        return
      case WS_CLOSE.CONTROL_BACKLOG:
        this.retry(code, backoffDelay(this.attempt + 1, this.env.random()), 'backlog')
        return
      case WS_CLOSE.MESSAGE_TOO_BIG:
        this.ctrl.push({ op: 'defect', code, message: 'the server closed the link with 1009 (message too big)' })
        this.retry(code, backoffDelay(this.attempt + 1, this.env.random()), 'too_big')
        return
      case WS_CLOSE.ABNORMAL:
        this.retry(code, backoffDelay(this.attempt + 1, this.env.random()))
        this.triage()
        return
    }
    this.retry(code, backoffDelay(this.attempt + 1, this.env.random()))
  }

  private retry(code: number, delay: number, error?: ConnErrorKind): void {
    this.attempt++
    this.setConn(C_RECONNECTING, { code, nextInMs: delay, error })
    this.tRetry = this.env.setTimeout(() => {
      this.tRetry = null
      this.reconnects++
      this.connect()
    }, delay)
  }

  private fatal(code: number, error: ConnErrorKind): void {
    this.clearTimers()
    this.setConn(C_FATAL, { code, error })
    const s = this.sock
    this.sock = null
    s?.close(WS_CLOSE.NORMAL)
  }

  /** 4401, 4403 or whoami 401: ask the main thread for a fresh token (same principal_hint) and reconnect at once */
  private needToken(code: number, error: ConnErrorKind | undefined): void {
    if (++this.authFails > AUTH_RETRY_MAX) {
      this.fatal(code, 'auth')
      return
    }
    this.clearTimer('tRetry')
    this.waitingToken = true
    this.setConn(C_RECONNECTING, { code, nextInMs: 0, error })
    this.ctrl.push({ op: 'needToken', role: this.role, code })
    this.scheduleCtrl()
    this.tToken = this.env.setTimeout(() => {
      this.tToken = null
      if (!this.waitingToken) return
      this.waitingToken = false
      this.reconnects++
      this.connect()
    }, TOKEN_WAIT_MS)
  }

  /** 1006: the browser hides the HTTP status of a refused upgrade; whoami tells 401 from 403 (AWR-17 §3.3 item 4) */
  private triage(): void {
    if (!this.env.checkAuth || this.url.startsWith('fake:')) return
    const token = this.token
    this.env.checkAuth(this.url, token).then((status) => {
      if (this.closedByUs || this.conn === C_FATAL || this.helloSent) return
      if (status === 403) {
        this.abortSilently()
        this.fatal(WS_CLOSE.ABNORMAL, 'auth')
      } else if (status === 401) {
        this.abortSilently()
        this.needToken(WS_CLOSE.ABNORMAL, undefined)
      }
    }, () => {})
  }

  private abortSilently(): void {
    const s = this.sock
    this.sock = null
    if (s) {
      s.onclose = null
      s.onmessage = null
      try {
        s.close()
      } catch {
        // already closed
      }
    }
    this.clearTimers()
  }

  private clearTimer(k: 'tConnect' | 'tRetry' | 'tPing' | 'tStale' | 'tCtrl' | 'tStats' | 'tToken'): void {
    const h = this[k]
    if (h !== null) this.env.clearTimeout(h)
    this[k] = null
  }
  private clearTimers(): void {
    this.clearTimer('tConnect')
    this.clearTimer('tRetry')
    this.clearTimer('tPing')
    this.clearTimer('tStale')
    this.clearTimer('tStats')
    this.clearTimer('tToken')
  }

  private setConn(c: number, info: { code?: number; nextInMs?: number; error?: ConnErrorKind } = {}): void {
    if (c === this.conn && info.code === undefined && info.error === this.lastError) return
    this.conn = c
    if (c === C_SYNCING || c === C_LIVE) this.lastError = undefined
    else if (info.error !== undefined || c === C_RECONNECTING || c === C_FATAL) this.lastError = info.error
    this.dec.flags |= SF.CONN_CHANGED
    const m: CtrlMsg = { op: 'conn', state: CONN_STATES[c], attempt: this.attempt, nextInMs: info.nextInMs ?? 0, code: info.code ?? this.lastCloseCode }
    if (this.lastError) m.error = this.lastError
    this.ctrl.push(m)
    this.scheduleCtrl()
  }

  private sendJson(m: Record<string, unknown>): void {
    const s = this.sock
    if (s && s.readyState === WS_OPEN) s.send(JSON.stringify(m))
  }

  // ------------------------------------------------------------ socket messages
  private onSocketMessage(data: string | ArrayBuffer): void {
    const now = this.env.now()
    const dec = this.dec
    dec.roll(now)
    if (typeof data === 'string') {
      dec.countBytes(data.length)
      let m: CtrlMsg
      try {
        m = JSON.parse(data) as CtrlMsg
      } catch {
        return
      }
      if (m && typeof m === 'object' && typeof m.op === 'string') this.onCtrl(m, now)
      return
    }
    dec.countBytes(data.byteLength)
    if (data.byteLength < 1) return
    const dv = new DataView(data) // one view per received message (M11 §6.4.14 point 4)
    const op = dv.getUint8(0)
    if (op === OP_TIME) {
      if (data.byteLength >= 24) this.onTime(dv, now)
      else dec.malformedFrames++
      return
    }
    if (op !== OP_BATCH) return // unknown opcode: ignored (CLIENT_DATA is C->S only)
    if (data.byteLength < 16) {
      dec.malformedFrames++
      return
    }
    readFrameHeader(dv, this.fh)
    if (this.fh.epoch !== this.curEpoch) {
      if (this.stash) dec.droppedEpochFrames++
      this.stash = data
      return
    }
    this.index(data, dv, now)
    this.maybeFlush()
    // no frame loop pulling: msgpack/json channels (roster, env, perf, sys) still reach the main thread on the timer
    if (!this.pullPending && this.tCtrl === null && this.dec.dataPending > 0) this.scheduleCtrl()
  }

  private index(buf: ArrayBuffer, dv: DataView, now: number): void {
    const snapshot = (this.fh.flags & BATCH_SNAPSHOT) !== 0
    this.dec.indexBatch(buf, dv, this.fh, this.rv, now)
    if (snapshot && this.conn === C_SYNCING) this.setConn(C_LIVE)
  }

  private onTime(dv: DataView, now: number): void {
    const t = this.time
    readTime(dv, 0, t)
    this.timeRecvAt = now
    const dec = this.dec
    if (t.epoch !== this.curEpoch) {
      this.curEpoch = t.epoch
      dec.clearRefs()
      dec.latestTSimMs = Number.NaN
      dec.flags |= SF.EPOCH_CHANGED
    }
    if (this.stash) {
      // every TIME judges the stashed frame: same epoch passes, anything else was an old-epoch frame
      const st = this.stash
      this.stash = null
      const sdv = new DataView(st)
      readFrameHeader(sdv, this.fh)
      if (this.fh.epoch === this.curEpoch) this.index(st, sdv, now)
      else dec.droppedEpochFrames++
    }
    dec.flags |= SF.TIME_CHANGED
    if (this.helloSent && this.conn === C_DEGRADED) this.setConn(C_LIVE)
    if (this.helloSent && this.conn === C_SYNCING && this.subs.size === 0) this.setConn(C_LIVE)
    this.maybeFlush()
  }

  // ------------------------------------------------------------ control messages
  private onCtrl(m: CtrlMsg, now: number): void {
    switch (m.op) {
      case 'serverInfo':
        this.onServerInfo(m)
        break
      case 'advertise':
        for (const c of (m.channels as Record<string, unknown>[] | undefined) ?? []) this.dec.ch.advertise(c)
        break
      case 'unadvertise':
        for (const id of (m.ids as number[] | undefined) ?? []) this.dec.ch.unadvertise(Number(id))
        break
      case 'subscribed':
        this.dec.ch.onSubscribed(Number(m.id), Number(m.rate) || 0, ((m.channels as number[] | undefined) ?? []).map(Number), m.added === true)
        this.pushCtrl(m)
        break
      case 'pong':
        this.clock.onPong(Number(m.t), now, Number(m.server_ns) / 1e6)
        break
      case 'event':
        this.lastEventSeq = Math.max(this.lastEventSeq, Number(m.seq) || 0)
        this.pushEvent(m)
        break
      case 'events': {
        const items = (m.items as Record<string, unknown>[] | undefined) ?? []
        for (const it of items) this.lastEventSeq = Math.max(this.lastEventSeq, Number(it.seq) || 0)
        this.pushEvent(m)
        break
      }
      case 'result': {
        const c = this.calls.get(String(m.id))
        if (c) {
          c.answered = true
          if (c.timer !== null) this.env.clearTimeout(c.timer)
          c.timer = null
          if (m.final === true) this.calls.delete(c.id)
        }
        this.pushCtrl(m)
        break
      }
      default:
        this.pushCtrl(m) // progress, status, removeStatus, error, playbackState and unknown ops (ignored by the client)
    }
  }

  private onServerInfo(m: CtrlMsg): void {
    if (this.helloSent) {
      this.pushCtrl(m) // re-sent inside the connection (mode, world or run change): no second hello
      return
    }
    const layouts = (m.layouts ?? {}) as Record<string, string>
    const bad = DECODED_SCHEMAS.filter((s) => layouts[s] !== undefined && layouts[s] !== SCHEMA_HASH[s])
    const major = str(m.contracts).split('.')[0]
    if (bad.length || major !== CONTRACTS_VERSION.split('.')[0]) {
      this.pushCtrl({ op: 'error', code: bad.length ? Reason.LAYOUT_MISMATCH : Reason.PROTOCOL_UNSUPPORTED, name: bad.length ? 'LAYOUT_MISMATCH' : 'PROTOCOL_UNSUPPORTED',
        message: bad.length ? `layout hash differs: ${bad.join(', ')}` : `contracts ${String(m.contracts)}`, ref: { op: 'serverInfo' } })
      this.fatal(WS_CLOSE.NORMAL, 'version')
      return
    }
    const sid = str(m.sessionId)
    const same = sid !== '' && sid === this.sessionId
    if (!same) {
      // a new gateway instance: channel ids, event sequence and clock samples start over (AWR-17 §6.13 item 2)
      if (this.sessionId !== '') {
        this.dec.ch.clear()
        this.dec.clearRefs()
      }
      this.clock.reset()
      this.lastEventSeq = 0
      this.sessionId = sid
    }
    this.clock.sent = 0 // every connection starts with the 5 x 100 ms burst (samples kept within the same session)
    this.pushCtrl({ op: 'connected', sessionId: sid, sameSession: same })
    this.pushCtrl(m)
    const hello: Record<string, unknown> = { op: 'hello', client: CLIENT_NAME, contracts: CONTRACTS_VERSION, tier: this.tier, deviceClass: this.deviceClass }
    if (same && this.lastEventSeq > 0) hello.resume = { sessionId: sid, lastEventSeq: this.lastEventSeq }
    if (this.role === 'viewer') hello.role = 'viewer'
    this.sendJson(hello)
    this.helloSent = true
    this.attempt = 0
    this.proto1002 = 0
    this.authFails = 0
    this.setConn(C_SYNCING)
    this.schedulePing()
    this.tStale = this.env.setTimeout(() => this.staleCheck(), STALE_AFTER_MS / 4)
    this.tStats = this.env.setTimeout(() => this.sendStats(), CLIENT_STATS_MS)
    if (this.subs.size) this.sendJson({ op: 'subscribe', subs: [...this.subs.values()].map(subWire) })
    for (const c of this.calls.values()) {
      this.sendCall(c) // same call id after reconnect: the server answers duplicate with the latest result
      if (c.cancel) {
        c.cancel = false
        this.sendJson({ op: 'cancel', id: c.id })
      }
    }
  }

  private pushEvent(m: CtrlMsg): void {
    if (this.ctrl.pushEvent(m)) {
      this.dec.eventGaps++
      this.dec.flags |= SF.GAP
    }
    this.scheduleCtrl()
  }

  private pushCtrl(m: CtrlMsg): void {
    this.ctrl.push(m)
    this.scheduleCtrl()
  }

  private scheduleCtrl(): void {
    if (this.pullPending && this.free.length) {
      this.flush()
      return
    }
    if (this.tCtrl !== null) return
    this.tCtrl = this.env.setTimeout(() => {
      this.tCtrl = null
      this.deliverCtrl()
    }, this.hidden ? CTRL_HIDDEN_MS : CTRL_FLUSH_MS)
  }

  /** no frame loop is pulling (hidden page, tests): deliver control messages without a slot */
  private deliverCtrl(): void {
    if (this.pullPending && this.free.length) {
      this.flush()
      return
    }
    this.dec.takeData(this.sink)
    const list = this.hidden ? this.ctrl.takeNonEvents() : this.ctrl.take()
    if (list) this.env.post({ slot: null, ctrl: list }, [])
  }

  // ------------------------------------------------------------ calls
  private call(m: Extract<WorkerIn, { cmd: 'call' }>): void {
    const timeoutMs = Math.max(1, Math.min(30_000, Math.round(m.timeoutMs)))
    const c: CallSpec = { id: m.id, service: m.service, args: m.args, timeoutMs, confirm: m.confirm, timer: null, answered: false, cancel: false }
    if (!this.helloSent || !this.sock) {
      // no queueing while disconnected (AWR-17 §6.13 item 3): the UI shows the command unavailable
      const r = REASONS[Reason.SERVICE_UNAVAILABLE]
      this.pushCtrl({ op: 'result', id: m.id, status: 'rejected', code: r.code, reason: r.name, message: r.message_zh, remedy: r.remedy_zh, final: true,
        effect: { status: 'UNAVAILABLE', verify_trust: 0 } })
      return
    }
    this.calls.set(c.id, c)
    this.sendCall(c)
  }
  private sendCall(c: CallSpec): void {
    const args = c.confirm ? { ...(c.args as Record<string, unknown>), confirm_token: c.confirm } : c.args
    this.sendJson({ op: 'call', id: c.id, service: c.service, args, timeout_ms: c.timeoutMs })
    if (c.timer === null && !c.answered) {
      c.timer = this.env.setTimeout(() => {
        c.timer = null
        if (c.answered || !this.calls.has(c.id)) return
        this.calls.delete(c.id)
        const r = REASONS[Reason.ACK_TIMEOUT]
        this.pushCtrl({ op: 'result', id: c.id, status: 'timeout', code: r.code, reason: r.name, message: r.message_zh, remedy: r.remedy_zh, final: true,
          effect: { status: 'UNAVAILABLE', verify_trust: 0 } })
      }, c.timeoutMs)
    }
  }

  // ------------------------------------------------------------ client publish (CLIENT_DATA, AWR-17 §6.4)
  private setpoint(m: Extract<WorkerIn, { cmd: 'setpoint' }>): void {
    const s = this.sock
    if (!this.helloSent || !s || s.readyState !== WS_OPEN) return // no queueing: the watchdog on the server stops the vehicle
    let e = this.setpointCh.get(m.agentNo)
    if (!e) {
      if (this.nextClientCh > CLIENT_CH_MAX) return
      e = { ch: this.nextClientCh++, seq: 0 }
      this.setpointCh.set(m.agentNo, e)
      this.sendJson({ op: 'advertise', channels: [{ id: e.ch, topic: `uav/${m.id}/setpoint`, encoding: 'raw', schemaName: 'awr.VelSetpoint16.v1' }] })
    }
    e.seq = (e.seq + 1) >>> 0
    writeClientData(this.cdView, e.ch, e.seq, Math.round(this.simNowNs()), m.final ? CD_FINAL : 0, m.vx, m.vy, m.vz, m.yawRate)
    s.send(this.cdBuf) // send() copies the bytes: the 32 B frame is reused
    if (m.final) {
      this.sendJson({ op: 'unadvertise', ids: [e.ch] })
      this.setpointCh.delete(m.agentNo)
    }
  }

  /** client estimate of the simulation time (AWR-17 §6.10): t_sim + rate * (srvNow - t_srv) while advancing */
  private simNowNs(): number {
    const t = this.time
    const off = this.clock.offsetMs
    if (!Number.isFinite(off) || !advancing(t.state)) return t.t_sim_ns
    const srvNowMs = this.env.now() + off
    return t.t_sim_ns + t.rate * (srvNowMs - t.t_srv_ns / 1e6) * 1e6
  }

  // ------------------------------------------------------------ timers
  private schedulePing(): void {
    this.tPing = this.env.setTimeout(() => {
      this.tPing = null
      if (!this.helloSent) return
      const msg: Record<string, unknown> = { op: 'ping', t: this.env.now() }
      if (Number.isFinite(this.clock.srttMs)) msg.srttMs = round3(this.clock.srttMs)
      this.sendJson(msg)
      this.clock.sent++
      this.schedulePing()
    }, this.clock.sent === 0 ? 0 : this.clock.nextGapMs())
  }

  private staleCheck(): void {
    this.tStale = this.env.setTimeout(() => this.staleCheck(), STALE_AFTER_MS / 4)
    const now = this.env.now()
    if (this.conn === C_LIVE && now - this.timeRecvAt > STALE_AFTER_MS) this.setConn(C_DEGRADED)
  }

  private sendStats(): void {
    this.tStats = this.env.setTimeout(() => this.sendStats(), CLIENT_STATS_MS)
    this.rollPulls(this.env.now())
    const st = this.mainStats
    const m: Record<string, unknown> = { op: 'clientStats', fps: round3(this.fps()), decodeMs: round3(this.decodeMs), tier: this.tier, deviceClass: this.deviceClass }
    for (const k of ['frameMs', 'frameP95Ms', 'heapMB', 'latencyP95Ms', 'dGlobalMs'] as const) if (Number.isFinite(st[k])) m[k] = round3(st[k]!)
    for (const k of ['droppedFrames', 'pointBudget'] as const) if (Number.isFinite(st[k])) m[k] = Math.round(st[k]!)
    this.sendJson(m)
  }

  private rollPulls(now: number): void {
    if (this.pullWinStart === 0) this.pullWinStart = now
    const dt = now - this.pullWinStart
    if (dt < 1000) return
    this.pullFps = (this.winPulls * 1000) / dt
    this.winPulls = 0
    this.pullWinStart = now
  }

  /** consumption rate: the main-thread frame rate when the client reports it, else the rate of returned slots */
  private fps(): number {
    const f = this.mainStats.fps
    return f !== undefined && Number.isFinite(f) ? f : this.pullFps
  }

  // ------------------------------------------------------------ pull, flush, ack
  private onPull(returned: ArrayBuffer | null): void {
    const now = this.env.now()
    if (returned) {
      // slots come back in the order they were sent: pop the frameSeqMax recorded for this one
      if (this.sentN > 0) {
        const seq = this.sentSeq[(this.sentHead - this.sentN + 8) % 8]
        this.sentN--
        if (seq > this.consumed) this.consumed = seq
      }
      this.free.push(returned)
      this.winPulls++
      this.rollPulls(now)
      this.maybeAck(now)
    }
    this.pullPending = true
    if (this.dec.dirtyN > 0 || this.dec.flags !== 0 || this.ctrl.length || this.ctrl.dropped) this.flush()
  }

  private maybeFlush(): void {
    if (this.pullPending && this.free.length && (this.dec.dirtyN > 0 || this.dec.flags !== 0 || this.ctrl.length)) this.flush()
  }

  private maybeAck(now: number): void {
    if (this.consumed - this.lastAck >= ACK_EVERY_FRAMES || (this.consumed > this.lastAck && now - this.lastAckAt >= ACK_MAX_GAP_MS)) {
      const m: Record<string, unknown> = { op: 'ack', frame: this.consumed, fps: round3(this.fps()), decodeMs: round3(this.decodeMs) }
      if (Number.isFinite(this.lastAgeMs)) m.lagMs = round3(this.lastAgeMs)
      this.sendJson(m)
      this.lastAck = this.consumed
      this.lastAckAt = now
    }
  }

  private flush(): void {
    const buf = this.free.pop()
    if (!buf) return
    const env = this.env
    const t0 = env.now()
    const w = this.writer
    const dv = w.dv
    const dec = this.dec
    const o = dec.compose(w, this.sink)
    const t = this.time
    const offW = this.clock.offsetMs
    dv.setUint32(H.magic, SLOT_MAGIC, true)
    dv.setUint32(H.slotNo, this.sentHead % 3, true)
    dv.setUint32(H.frameSeqMax, dec.seqMax, true)
    dv.setUint16(H.epoch, this.curEpoch & 0xffff, true)
    dv.setUint16(H.flags, dec.flags, true)
    dv.setFloat64(H.frameTSimMs, dec.frameTSimMs, true)
    dv.setUint32(H.swarmN, o.swarmN, true)
    dv.setUint32(H.swarmSeq, o.swarmSeq, true)
    dv.setFloat64(H.swarmTSimMs, o.swarmTSimMs, true)
    dv.setUint32(H.fullCount, o.fullN, true)
    dv.setUint32(H.rawCount, o.rawN, true)
    dv.setUint32(H.resetCount, dec.resetN, true)
    dv.setUint32(H.ctrlCount, this.ctrl.length, true)
    dv.setFloat64(H.timeTSimMs, t.t_sim_ns / 1e6, true)
    dv.setFloat64(H.timeTSrvMs, t.t_srv_ns / 1e6, true)
    dv.setFloat32(H.timeRate, t.rate, true)
    dv.setUint8(H.timeState, (t.state & 0x0f) | (t.replay ? 0x80 : 0))
    dv.setUint8(H.connState, this.conn)
    dv.setUint16(H.timeEpoch, t.epoch & 0xffff, true)
    dv.setFloat64(H.clockOffsetMainMs, this.clock.offsetMain(this.originMain, env.timeOrigin), true)
    dv.setFloat64(H.srttMs, this.clock.srttMs, true)
    // display-delay sample (M11 §6.3.8 ageMs): only for fresh data while the clock advances
    let age = Number.NaN
    if (o.fresh && Number.isFinite(dec.latestTSimMs) && Number.isFinite(offW) && t.rate > 0 && advancing(t.state)) {
      const srvNow = t0 + offW
      age = srvNow - (t.t_srv_ns / 1e6 + (dec.latestTSimMs - t.t_sim_ns / 1e6) / t.rate)
      this.lastAgeMs = age
    }
    dv.setFloat64(H.ageMs, age, true)
    dv.setFloat64(H.bytesPerS, dec.bytesPerS, true)
    dv.setFloat32(H.swarmHz, dec.swarmHz, true)
    dv.setFloat32(H.selHz, dec.selHz, true)
    dv.setUint32(H.reconnects, this.reconnects, true)
    dv.setUint32(H.droppedEpochFrames, dec.droppedEpochFrames, true)
    for (let i = 0; i < RESET_MAX; i++) dv.setUint16(H.resetChannelIds + 2 * i, i < dec.resetN ? dec.resetIds[i] : 0, true)
    const shift = env.timeOrigin - this.originMain
    dv.setFloat64(H.swarmRecvMainMs, o.swarmN > 0 ? dec.swarmRecvAt + shift : 0, true)
    dv.setFloat32(H.focusHz, dec.focusHz, true)
    dv.setFloat32(H.selJitterMs, dec.selJitterMs, true)
    dv.setUint32(H.eventGaps, dec.eventGaps, true)
    dv.setFloat64(H.timeRecvMainMs, Number.isFinite(this.timeRecvAt) ? this.timeRecvAt + shift : 0, true)
    dv.setUint32(H.malformedFrames, dec.malformedFrames, true)
    this.decodeMs = env.now() - t0
    dv.setFloat64(H.decodeMs, this.decodeMs, true)
    w.copyTo(buf)
    const delivered = dec.seqMax
    this.sentSeq[this.sentHead % 8] = dec.seqMax
    this.sentHead++
    if (this.sentN < 8) this.sentN++
    dec.flags = 0
    dec.resetN = 0
    dec.seqMax = 0
    dec.latestTSimMs = Number.NaN
    this.pullPending = false
    const ctrl = this.hidden ? this.ctrl.takeNonEvents() : this.ctrl.take()
    this.clearTimer('tCtrl')
    env.post({ slot: buf, ctrl }, [buf])
    // the slot answers the main thread's pull of this frame: its frames count as consumed now (FX2-R3, AWR-17 §6.9 L1
    // revised in ADR-067); waiting for the slot to come back added one frame interval to every ack, and at 15-30 fps the
    // 60 Hz selected-vehicle channel ran into the credit window (credit_skips about half of the ticks, D1-AC-26)
    if (delivered > this.consumed) this.consumed = delivered
    this.maybeAck(t0)
  }
}

function advancing(state: number): boolean {
  const s = state & 0x0f
  return s === TimeState.PLAYING || s === TimeState.LIVE
}

function subWire(s: SubSpec): Record<string, unknown> {
  const w: Record<string, unknown> = { id: s.id, topic: s.topic, rate: s.rate, mode: s.mode }
  if (s.priority !== undefined) w.priority = s.priority
  if (s.filter) w.filter = { ...s.filter }
  return w
}
