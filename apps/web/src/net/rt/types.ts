// Public types of the realtime client (M11 §7.5; AWR-17 §6.3, §6.12, §7.2). Owner: M11. Wire field names follow AWR-17
// (envelope camelCase, business payloads snake_case); the TelemetryFrame slot layout is in ./frame.ts.
import type { SwarmSoA } from './layouts'

/** string of a wire value (strings pass, numbers are formatted, anything else becomes the default) */
export const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : typeof v === 'number' || typeof v === 'boolean' ? String(v) : d)

export type RateClass = 0 | 1 | 2 | 5 | 10 | 15 | 20 | 30 | 60
/** client connection states (M11 §6.6.3; AWR-14 §7.7); the numeric code is the index in CONN_STATES */
export type ConnState = 'IDLE' | 'CONNECTING' | 'SYNCING' | 'LIVE' | 'DEGRADED' | 'RECONNECTING' | 'FATAL' | 'CLOSED'
export const CONN_STATES: readonly ConnState[] = ['IDLE', 'CONNECTING', 'SYNCING', 'LIVE', 'DEGRADED', 'RECONNECTING', 'FATAL', 'CLOSED']
export type CallStatus = 'accepted' | 'running' | 'succeeded' | 'rejected' | 'failed' | 'timeout' | 'canceled'

export interface SubscribeOpts {
  rate: RateClass
  mode?: 'latest' | 'all'
  priority?: 0 | 1 | 2 | 3
  filter?: { types?: readonly string[]; levelMin?: 0 | 1 | 2 | 3 }
}

/** one reliable event (AWR-17 §6.12; `event` op or an item of `events`) */
export interface RtEvent {
  seq: number
  t_sim_ns: number
  t_wall_ns?: string
  type: string
  level: 0 | 1 | 2 | 3
  producer?: string
  uav?: string | null
  cid?: string | null
  data: Record<string, unknown>
}

/** persistent banner (`status` op, AWR-17 §6.3); removed by `removeStatus` */
export interface StatusItem {
  id: string
  level: 'info' | 'warning' | 'error'
  message: string
  source?: string
  code?: number
}

/** effect record (packages/contracts/agent/effect.schema.json; AWR-17 §7.3) */
export interface Effect {
  status: 'OK' | 'UNVERIFIED' | 'FAILED' | 'UNAVAILABLE' | 'PAYMENT_REQUIRED'
  verify_trust: number
  simulated?: boolean
  native_ack?: boolean
  protocol?: string
  requested?: string
  observed_state?: string
  latency_ms?: number
  message?: string
  metrics?: Record<string, number>
}

/** `result` op without the envelope op field (AWR-17 §7.2) */
export interface CallResult {
  id: string
  status: CallStatus
  code: number
  reason?: string
  message?: string
  detail?: unknown
  remedy?: string
  final?: boolean
  duplicate?: boolean
  dup_of?: string
  warnings?: string[]
  retry_after_ms?: number
  effect?: Effect
  data?: Record<string, unknown>
}
export interface Progress {
  id: string
  data: { phase?: 'admitted' | 'planning' | 'executing' | 'paused'; [k: string]: unknown }
}
export interface CallHandle {
  readonly id: string
  /** resolves with the final result (succeeded, failed, canceled, rejected, timeout) */
  readonly result: Promise<CallResult>
  /** every result in order: accepted, running, then the final one */
  onResult(cb: (r: CallResult) => void): void
  onProgress(cb: (p: Progress) => void): void
  cancel(): void
}

export interface ServerInfoView {
  readonly sessionId: string
  readonly connId: string
  readonly worldId: string
  readonly contentVersion: string
  readonly runId: string
  readonly segment: number
  readonly mode: 'live' | 'replay'
  readonly clock: { mode: string; pausable: boolean; maxSpeed: number; steppable: boolean }
  readonly role: 'viewer' | 'operator' | 'admin'
  readonly seat: 'held' | 'none' | 'other'
  readonly window: number
  readonly layouts: Readonly<Record<string, string>>
  readonly principal: string
  readonly capabilities: readonly string[]
}

export interface RosterEntry {
  agentNo: number
  id: string
  model: string
  kind: string
  producer: string
  simulated: boolean
  lifecycle: string
}
export interface RosterView {
  readonly size: number
  readonly version: number
  get(agentNo: number): RosterEntry | undefined
  idOf(agentNo: number): string | undefined
  agentNoOf(id: string): number
  entries(): readonly RosterEntry[]
}

/**
 * Why the link is down or degraded (AWR-14 §7.6, §7.7): `version` E-07 (4426, contracts major or layout hash mismatch),
 * `auth` E-08 (401 after a token refresh, or 403 from whoami after 1006), `backlog` E-09 (1013), `seat_revoked` E-16
 * (4403, reconnecting as viewer), `conn_limit` E-17 (4429, retry after 30 s), `protocol` (1002 three times in a row),
 * `policy` (1008), `too_big` (1009, reported as a defect).
 */
export type ConnErrorKind = 'version' | 'auth' | 'backlog' | 'seat_revoked' | 'conn_limit' | 'protocol' | 'policy' | 'too_big'
/** AWR-14 §7.6 UI error identifiers of the error kinds (front-end only, never on the wire) */
export const CONN_ERROR_UI: Readonly<Record<ConnErrorKind, string>> = {
  version: 'E-07', auth: 'E-08', backlog: 'E-09', seat_revoked: 'E-16', conn_limit: 'E-17', protocol: 'E-07', policy: 'E-07', too_big: 'E-09',
}
export interface ConnInfo {
  /** reconnect attempt counter (0 after a successful handshake) */
  attempt: number
  /** delay until the next attempt (RECONNECTING), ms */
  nextInMs: number
  /** last WebSocket close code */
  code?: number
  /** classified cause, when the close code or handshake has one */
  error?: ConnErrorKind
  /** main-thread performance.now() when this state was entered (offline banner after 1 s, AWR-14 §7.7) */
  sinceMs?: number
  /** main-thread performance.now() of the last LIVE period end or current LIVE start ("offline, updated 12 s ago") */
  lastLiveMs?: number
}

/** decoded TIME (AWR-17 §6.4), ms relative to the run and to gw_t0; the object is reused */
export interface TimeFrameView {
  state: number
  epoch: number
  rate: number
  tSimMs: number
  tSrvMs: number
}

/** header fields of the current TelemetryFrame slot (M11 §6.3.8); reused object, valid until the next swapFrame() */
export interface FrameHeaderView {
  slotNo: number
  frameSeqMax: number
  epoch: number
  flags: number
  frameTSimMs: number
  swarmN: number
  swarmSeq: number
  swarmTSimMs: number
  fullCount: number
  rawCount: number
  resetCount: number
  timeTSimMs: number
  timeTSrvMs: number
  timeRate: number
  timeState: number
  connState: number
  timeEpoch: number
  clockOffsetMainMs: number
  srttMs: number
  decodeMs: number
  ageMs: number
  bytesPerS: number
  swarmHz: number
  selHz: number
  focusHz: number
  reconnects: number
  droppedEpochFrames: number
  eventGaps: number
  swarmRecvMainMs: number
  /** receive time of the latest TIME in the worker, main-thread time base (reserved area, offset 216) */
  timeRecvMainMs: number
  /** BATCH frames that failed to parse (fuzz robustness, offset 224) */
  malformedFrames: number
}

/** Full64 items of the slot (M11 §6.3.8): item k at base + 80k holds agentNo u16, channelId u16, seq u32,
 * tSampleMs f64 and the raw awr.DroneState64.v1 bytes at +16 */
export interface FullRecordsView {
  count: number
  bytes: DataView
  base: number
}
/** small raw items (EnvSample32, SensorPose48): agentNo u16, channelId u16, schema u8, pad, len u16, tSampleMs f64, bytes at +16 */
export interface RawRecordsView {
  count: number
  bytes: DataView
  base: number
}

export interface TelemetryFrame {
  readonly hdr: FrameHeaderView
  readonly swarm: SwarmSoA
  readonly full: FullRecordsView
  readonly raw: RawRecordsView
  readonly resetChannelIds: Uint16Array
}

/** msgpack or json channel data delivered on the control path (roster, env/state, state_ext, perf/server, ...) */
export interface DataMsg {
  topic: string
  channelId: number
  seq: number
  tSimMs: number
  data: unknown
}

export interface RtInitOptions {
  url: string
  token: string
  tier: 'A' | 'B' | 'S'
  deviceClass: 'dGPU' | 'iGPU' | 'software'
}

export interface RtClient {
  init(o: RtInitOptions): void
  /** reference counted; the same topic subscribed twice uses the highest rate; returns the release function */
  subscribe(topic: string, o: SubscribeOpts): () => void
  /**
   * telemetry phase only: the TelemetryFrame when a new slot arrived since the last call, else null (AD-06). The frame
   * object and its typed views are the same on every call (the slot is copied into them), so callers may keep them.
   */
  swapFrame(): TelemetryFrame | null
  call(service: string, args: object, o?: CallOptions): CallHandle
  /** fleet/cmd/{op} for several vehicles (ids or "*"): one summary result, counts in progress, final when all are terminal */
  callBatch(op: BatchOp, vehicles: readonly string[] | '*', args?: object, o?: CallOptions): CallHandle
  /** teleoperation setpoint (CLIENT_DATA, AWR-17 §6.4); needs a running uav/{id}/cmd/velocity call of this connection */
  publishSetpoint(agentNo: number, vx: number, vy: number, vz: number, yawRate: number, final?: boolean): void
  /** replay control (AWR-17 §6.11, D1-ext); resolves with the playbackState that carries the same request_id */
  playback(cmd: PlaybackCmd, args?: PlaybackArgs): Promise<PlaybackState>
  onPlaybackState(cb: (s: PlaybackState) => void): () => void
  /** at most one batch per frame (AD-03) */
  onEvents(cb: (batch: readonly RtEvent[]) => void): () => void
  /** the worker dropped events while the page was hidden: backfill with GET /api/events?since=fromSeq (M11-FR-095) */
  onEventGap(cb: (g: EventGap) => void): () => void
  onStatus(cb: (items: readonly StatusItem[]) => void): () => void
  /** `error` ops for non-call requests (subscribe 314/315, CLIENT_DATA 322, ...) */
  onError(cb: (e: RtErrorMsg) => void): () => void
  onConnState(cb: (s: ConnState, info: ConnInfo) => void): () => void
  /** TIME changed; recvMs is the worker receive time of that TIME in the main-thread time base */
  onTime(cb: (t: TimeFrameView, recvMs: number) => void): () => void
  onData(cb: (m: DataMsg) => void): () => void
  readonly status: ConnState
  readonly connInfo: ConnInfo
  readonly roster: RosterView
  readonly serverInfo: ServerInfoView | null
  onServerInfo(cb: (s: ServerInfoView) => void): () => void
  /** columns of the latest swarm SoA seen by swapFrame (stable between swapFrame calls) */
  readonly swarm: SwarmSnapshot
  /** fields for the 1 Hz clientStats op (frame timing, heap, point budget; AWR-17 §6.3) */
  setClientStats(s: ClientStats): void
  /** page visibility: while hidden the worker keeps events (at most 8192) and delivers the other control messages at 4 Hz */
  setHidden(hidden: boolean): void
  reconnectNow(): void
  /** reconnect now with a new token and role (seat acquired, or released back to viewer; AWR-14 §6.12) */
  reauthenticate(token: string, role: 'operator' | 'viewer'): void
  close(): void
}

export interface CallOptions {
  /** first-result timeout (default 3000 ms, AWR-17 §7.4) */
  timeoutMs?: number
  confirm?: string
  /** call id; generated when omitted (kept across reconnects so the gateway answers duplicate) */
  id?: string
}

/** ops allowed for fleet/cmd/{op} (AWR-12 §5.5 item 5; M11-FR-061) */
export type BatchOp = 'rtl' | 'land' | 'hover' | 'safety_stop' | 'pause' | 'resume' | 'takeoff'
export const BATCH_OPS: readonly BatchOp[] = ['rtl', 'land', 'hover', 'safety_stop', 'pause', 'resume', 'takeoff']
/** per-vehicle progress counts of a batch call (`progress.data.counts`, `fleet.batch.progress`) */
export interface BatchCounts { accepted: number; running: number; succeeded: number; failed: number; canceled: number; rejected: number }
/** the summary result data of a batch call (AWR-17 §7.5 item 5) */
export interface BatchSummary {
  accepted_n: number
  rejected_n: number
  rejected_by_code: Record<string, number>
  accepted: string[]
  rejected: [string, number][]
}

export type PlaybackCmd = 'open' | 'close' | 'play' | 'pause' | 'seek' | 'speed'
export interface PlaybackArgs { run?: string; segment?: number; seek_ns?: number; speed?: number; request_id?: string }
/** `playbackState` op (AWR-17 §6.3, §6.11) without the op field */
export interface PlaybackState {
  status: 'idle' | 'opening' | 'playing' | 'paused' | 'buffering' | 'ended' | 'error'
  request_id?: string
  current_ns?: number
  speed?: number
  did_seek?: boolean
  epoch?: number
  run?: string
  segment?: number
  dataStart_ns?: number
  dataEnd_ns?: number
  code?: number
  speed_max?: number
  loaded_until_ns?: number
  decimation_s?: number
  lineage?: { epoch: number; t_from_ns: number; t_to_ns: number }[]
  warnings?: string[]
}

/** `error` op of the gateway (AWR-17 §6.3) */
export interface RtErrorMsg { code: number; name: string; message: string; ref?: { op: string; id?: string | number } }

/** local event gap: seq range the worker dropped (inclusive) and how many events */
export interface EventGap { fromSeq: number; toSeq: number; dropped: number }

/** clientStats fields supplied by the main thread (the worker adds fps, decodeMs, tier, deviceClass) */
export interface ClientStats {
  /** main-thread frame rate (RtClient measures its swapFrame rate and reports it once per second) */
  fps?: number
  frameMs?: number
  frameP95Ms?: number
  heapMB?: number
  droppedFrames?: number
  pointBudget?: number
  latencyP95Ms?: number
  dGlobalMs?: number
}

/**
 * Latest swarm columns (views over the persistent TelemetryFrame image: the worker keeps the last decoded swarm in every
 * slot, so these always hold the most recent Lite32 rows; `version` changes when a new swarm sample arrived).
 */
export interface SwarmSnapshot {
  n: number
  seq: number
  tSimMs: number
  version: number
  agentNo: Uint16Array
  fs: Uint8Array
  battery: Uint8Array
  flags: Uint8Array
  ctrl: Uint8Array
  /** ENU m, 3 per row */
  pos: Float32Array
  /** ENU m/s, 3 per row */
  vel: Float32Array
  /** [x, y, z, w], 4 per row */
  quat: Float32Array
}
