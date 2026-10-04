// FakeSource (M11-FR-098, M11-AC-040; AWR-17 §6.13 item 6; AWR-03 D1-AC-35 TS part; AWR-18 §8.6 `source=fake`). Owner: M11.
// A socket-like stand-in for the gateway so the whole client path (rt.worker decode, slots, RtClient) runs without a
// backend. Two parts:
//   * FakeWorld: the "gateway + sim-core" shared by every connection of one fake URL (so a reconnect meets the same
//     session, event ring and call table): N in {1, 200, 1000} (any 1..1024) vehicles written with the generated layout
//     offsets (Lite32 swarm, Full64 per vehicle, roster and state_ext msgpack, EnvKeyframe, perf/server, sys/procs), the
//     60 Hz aligned tick, TIME at 10 Hz with the global epoch, reliable events with a 4096 ring (resume, gap), call
//     idempotency (`duplicate: true` for a re-sent id), goto/takeoff/land/rtl kinematics with accepted, running,
//     progress (<= 2 Hz) and succeeded, fleet/cmd/{op} batches (one summary result, counts in progress and
//     fleet.batch.progress, final when all children are terminal), velocity teleoperation fed by CLIENT_DATA with the
//     250 ms watchdog, and test hooks (epoch bump, gateway restart, TIME stall, event storm, status banners, drops);
//   * FakeSource: one connection (SocketLike): handshake of AWR-17 §6.2, subscriptions with rate classes, wildcard and
//     SNAPSHOT, per-connection scheduling with send-time assembly and the credit window (§6.8, §6.9), event filter.
//     Replay mode: the server-to-client records of an .awrrt fixture (AWR-16 §13.9) are delivered byte for byte at their
//     recorded times (loop optional); client messages are ignored.
// Deterministic: no Math.random; time comes from opts.now (default performance.now) and ticks from setInterval or tick().
import { encode as mpEncode } from '@msgpack/msgpack'
import { matchService, admitSymbol } from '@awr/contracts/commands'
import { Reason, REASONS } from '@awr/contracts/reasons'
import { PRESETS_JSON, PRESETS_SHA256, FIELD_PATHS } from '@awr/contracts/presets'
import {
  AWRT_S2C, AWRT_TEXT, BATCH_GAP, BATCH_SNAPSHOT, CD_FINAL, CONTRACTS_VERSION, DS64, ENC_MSGPACK, ENC_RAW, OP_BATCH, OP_CLIENT_DATA,
  RF_KEYFRAME, SCHEMA_HASH, SL32, TICK_HZ, FlightFlags, FlightState, Owner, PoseSrc, TimeState, packCtrl, packFs, pad8, quantizeRate,
  readAwrrt, setI64, writeTime, type AwrtRecord, type TimeView,
} from './layouts'
import { WS_CLOSED, WS_CONNECTING, WS_OPEN, type SocketClose, type SocketLike, type SocketMessage } from './transport'
import { BATCH_OPS, str, type BatchCounts, type BatchOp } from './types'

export interface FakeSourceOptions {
  /** number of synthetic vehicles, 1..1024 (D1 cases: 1, 200, 1000) */
  n?: number
  world?: string
  seed?: number
  /** credit window W (AWR-17 §6.9); 3..8 */
  window?: number
  /** simulation rate */
  rate?: number
  eventPeriodS?: number
  envPeriodS?: number
  /**
   * weather presets the environment keyframes cycle through every envPeriodS (default clear, partlyCloudy, overcast,
   * lightRain); 'all' in a fake URL is every preset of packages/contracts/env/presets.json (FX-UBO gpu-limits spec)
   */
  envPresets?: readonly string[]
  /** step keyframes (the new preset applies at once) instead of 30 s smooth transitions */
  envStep?: boolean
  /** number of passes through envPresets, after which the first preset applies once more and holds (default: endless) */
  envCycles?: number
  /** .awrrt bytes: replay mode */
  fixture?: ArrayBuffer | Uint8Array
  loop?: boolean
  /** false: the caller drives tick() (tests) */
  autoTick?: boolean
  /** monotonic ms */
  now?: () => number
  /** hover position of vehicle 0 when n = 1 (world ENU m) */
  start?: readonly [number, number, number]
  /** vehicles start on the ground (READY, not armed), so takeoff is needed before goto (like the real backend) */
  ground?: boolean
  /** close every connection with 1001 this often (reconnect UI development), s */
  dropEveryS?: number
  /** gateway instance id (serverInfo.sessionId) */
  sessionId?: string
  /** shared gateway state; a FakeSource without one creates its own */
  shared?: FakeWorld
}

/** `fake:?n=200&rate=1&window=6&ground=1&drop=30` (query parameters map to FakeSourceOptions; fixtures are fetched by the worker) */
export function parseFakeUrl(url: string): FakeSourceOptions & { fixtureUrl?: string } {
  const q = new URLSearchParams(url.slice(url.indexOf('?') + 1))
  const num = (k: string): number | undefined => (q.has(k) && Number.isFinite(Number(q.get(k))) ? Number(q.get(k)) : undefined)
  const start = q.get('start')?.split(',').map(Number)
  return {
    n: num('n'), world: q.get('world') ?? undefined, seed: num('seed'), window: num('window'), rate: num('rate'),
    eventPeriodS: num('eventPeriodS'), envPeriodS: num('envPeriodS'), loop: q.get('loop') === '1', ground: q.get('ground') === '1',
    envPresets: q.get('envPresets') === 'all' ? presets().presets.map((p) => p.id) : q.get('envPresets')?.split(',').filter((x) => x !== ''),
    envStep: q.get('envStep') === '1', envCycles: num('envCycles'),
    dropEveryS: num('drop'), sessionId: q.get('session') ?? undefined,
    start: start && start.length === 3 && start.every(Number.isFinite) ? [start[0], start[1], start[2]] : undefined,
    fixtureUrl: q.get('fixture') ?? undefined,
  }
}
export const isFakeUrl = (url: string): boolean => url.startsWith('fake:')

/**
 * Socket for a fake URL: replay fixtures get a fresh connection; synthesis connections of the same URL share one
 * FakeWorld from `cache`, so reconnects keep the session, the event ring and the call table (AWR-17 §6.12, §7.5).
 */
export function openFake(url: string, cache: Map<string, FakeWorld>, fixture?: ArrayBuffer | null): FakeSource {
  const o = parseFakeUrl(url)
  if (fixture) return new FakeSource({ ...o, fixture })
  let w = cache.get(url)
  if (!w) {
    w = new FakeWorld(o)
    cache.set(url, w)
  }
  return new FakeSource({ ...o, shared: w })
}

// ---------------------------------------------------------------- helpers
const FLAGS_AIR = FlightFlags.ARMED | FlightFlags.IN_AIR | FlightFlags.LOC_OK | FlightFlags.GCS_LINK | FlightFlags.FCU_LINK
const FLAGS_GROUND = FlightFlags.LOC_OK | FlightFlags.GCS_LINK | FlightFlags.FCU_LINK
const SUB_HOVER = 0
const SUB_GOTO = 1
const INFO_SCHEMAS = ['awr.DroneState64.v1', 'awr.SwarmLite32.v1', 'awr.EnvSample32.v1', 'awr.VelSetpoint16.v1', 'awr.SensorPose48.v1',
  'awr.rt.BatchHeader.v1', 'awr.rt.RecordHeader.v1', 'awr.rt.Time.v1', 'awr.rt.ClientDataHeader.v1']
const GOTO_DEFAULT_MPS = 8
const GOTO_TOL_M = 0.5
const CLIMB_MPS = 2
const DESCENT_MPS = 1.5
const GROUND_EPS_M = 0.05
const PROGRESS_EVERY_TICKS = TICK_HZ / 2 // progress <= 2 Hz (AWR-17 §7.4)
const TIME_EVERY_TICKS = TICK_HZ / 10 // TIME 10 Hz
const EVENTS_PER_MSG = 256
const EVENT_RING = 4096
/** idempotency table size (sim-core keeps 4096 for 60 s, AWR-17 §7.5 item 1) */
const RESULT_TABLE_CAP = 4096
const VELOCITY_WATCHDOG_MS = 250
const MODE_ORBIT = 0
const MODE_HOVER = 1
const MODE_NAV = 2
const MODE_VELOCITY = 3

function keyMatches(pattern: string, key: string): boolean {
  const p = pattern.split('/')
  const k = key.split('/')
  const m = (i: number, j: number): boolean => {
    if (i === p.length) return j === k.length
    if (p[i] === '**') {
      for (let jj = j; jj <= k.length; jj++) if (m(i + 1, jj)) return true
      return false
    }
    if (j === k.length) return false
    return (p[i] === '*' || p[i] === k[j]) && m(i + 1, j + 1)
  }
  return m(0, 0)
}
const clampI16 = (x: number): number => Math.max(-32768, Math.min(32767, Math.round(x)))
const round3 = (x: number): number => Math.round(x * 1000) / 1000
const reasonName = (code: number): string => REASONS[code]?.name ?? 'REJECTED'
const newCounts = (): BatchCounts => ({ accepted: 0, running: 0, succeeded: 0, failed: 0, canceled: 0, rejected: 0 })

interface PresetsLite {
  presets: { id: string; scalars: Record<string, Record<string, number>> }[]
  defaults: { scalars: Record<string, Record<string, number>>; config: Record<string, unknown> }
}
let presetsCache: PresetsLite | null = null
const presets = (): PresetsLite => (presetsCache ??= JSON.parse(PRESETS_JSON) as PresetsLite)
function presetVector(id: string): number[] {
  const p = presets()
  const d = p.defaults.scalars
  const sc = p.presets.find((x) => x.id === id)?.scalars ?? {}
  return FIELD_PATHS.map((path) => {
    const [g, k] = path.split('.')
    return Number(sc[g]?.[k] ?? d[g][k])
  })
}

interface Channel {
  id: number
  topic: string
  kind: 'state' | 'roster' | 'event'
  encoding: 'raw' | 'msgpack' | 'json'
  schemaName: string
  priority: number
  selfContained: boolean
  nativeHz: number
  defaultRate: number
  entity: { kind: string; id: string } | null
  row: number
  seq: number
  payload: Uint8Array | null
  tSimNs: number
  /** connections subscribed (lazy production of per-vehicle channels) */
  refs: number
}

/** per-connection scheduling state of one channel (AWR-17 §6.8 SubChan) */
interface SubState { refs: number; rate: number; period: number; lastSeq: number; snapshot: boolean; pending: boolean }

export interface FakeEvent { seq: number; t_sim_ns: number; t_wall_ns: string; type: string; level: number; producer: string; uav: string | null; cid: string | null; data: Record<string, unknown> }

interface BatchAgg { id: string; op: string; counts: BatchCounts; lastProgressK: number; dirty: boolean; done: boolean }
interface CallRec { last: Record<string, unknown>; final: boolean; conn: FakeSource | null }
type NavKind = 'goto' | 'takeoff' | 'land' | 'rtl' | 'velocity'
interface Nav {
  id: string
  kind: NavKind
  target: Float64Array
  speed: number
  tol: number
  t0Sim: number
  stage: 0 | 1
  lastProgressTick: number
  batch: BatchAgg | null
  /** rtl: land at home after arriving */
  land: boolean
  /** velocity: limits, frame, owner connection and watchdog */
  vmax: number
  body: boolean
  holdAlt: boolean
  lastSetpointAt: number
  conn: FakeSource | null
}

// ================================================================ FakeWorld (gateway + simulation)
export class FakeWorld {
  readonly n: number
  readonly ids: string[]
  readonly now: () => number
  readonly window: number
  readonly worldId: string
  readonly seed: number
  readonly rate: number
  sessionId: string
  readonly time: TimeView = { state: TimeState.LIVE, replay: false, epoch: 1, rate: 1, t_sim_ns: 0, t_srv_ns: 0 }
  readonly conns = new Set<FakeSource>()
  readonly channels: Channel[] = []
  readonly byId = new Map<number, Channel>()
  readonly stats = { ticks: 0, calls: 0, duplicates: 0, setpoints: 0, restarts: 0 }
  /** TIME not sent while true (DEGRADED tests) */
  stalled = false
  k = 0
  tSimNs = 0
  private readonly origin: number
  private readonly autoTick: boolean
  private timer: ReturnType<typeof setInterval> | null = null
  private readonly eventPeriodTicks: number
  private readonly envPeriodS: number
  private readonly envPresets: readonly string[]
  private readonly envStep: boolean
  private readonly envCycles: number
  private readonly dropEveryS: number
  private lastDropAt: number
  private connSeq = 0
  // events
  private gseq = 0
  readonly ring: FakeEvent[] = []
  // calls
  readonly results = new Map<string, CallRec>()
  private readonly batches: BatchAgg[] = []
  readonly statuses = new Map<string, Record<string, unknown>>()
  // vehicles (SoA)
  private readonly pos: Float64Array
  private readonly vel: Float64Array
  private readonly yaw: Float64Array
  private readonly yawRate: Float64Array
  private readonly mode: Uint8Array
  readonly fs: Uint8Array
  readonly flags: Uint8Array
  private readonly battery: Uint8Array
  private readonly nav: (Nav | null)[]
  private readonly liteBytes: Uint8Array
  private readonly fullBytes: Uint8Array
  private lastStepS = 0
  private envVersion = 0
  private envT0Ns = -1
  private envPreset = 'clear'
  private readonly swarmCh: Channel
  private readonly rosterCh: Channel
  private readonly envCh: Channel
  private readonly perfCh: Channel
  private readonly procsCh: Channel
  private readonly fullCh: Channel[] = []
  private readonly extCh: Channel[] = []

  constructor(o: FakeSourceOptions = {}) {
    this.now = o.now ?? (() => performance.now())
    this.origin = this.now()
    this.lastDropAt = this.origin
    this.worldId = o.world ?? 'shenzhen'
    this.seed = o.seed ?? 7
    this.sessionId = o.sessionId ?? `fake-${this.seed}`
    this.window = Math.min(8, Math.max(3, o.window ?? 6))
    this.rate = o.rate ?? 1
    this.time.rate = this.rate
    this.autoTick = o.autoTick !== false
    this.eventPeriodTicks = Math.max(1, Math.round((o.eventPeriodS ?? 2) * TICK_HZ))
    this.envPeriodS = o.envPeriodS ?? 20
    this.envPresets = o.envPresets && o.envPresets.length > 0 ? [...o.envPresets] : ['clear', 'partlyCloudy', 'overcast', 'lightRain']
    this.envStep = o.envStep === true
    this.envCycles = o.envCycles !== undefined && o.envCycles > 0 ? Math.floor(o.envCycles) : Number.POSITIVE_INFINITY
    this.dropEveryS = o.dropEveryS ?? 0
    const n = Math.max(1, Math.min(1024, Math.floor(o.n ?? 1)))
    this.n = n
    this.ids = Array.from({ length: n }, (_, a) => (n === 1 ? 'p600-01' : `uav${String(a + 1).padStart(4, '0')}`))
    this.pos = new Float64Array(3 * n)
    this.vel = new Float64Array(3 * n)
    this.yaw = new Float64Array(n)
    this.yawRate = new Float64Array(n)
    const ground = o.ground === true
    this.mode = new Uint8Array(n).fill(n === 1 || ground ? MODE_HOVER : MODE_ORBIT)
    this.fs = new Uint8Array(n).fill(ground ? packFs(FlightState.READY) : packFs(FlightState.FLYING, SUB_HOVER))
    this.flags = new Uint8Array(n).fill(ground ? FLAGS_GROUND : FLAGS_AIR)
    this.battery = new Uint8Array(n)
    this.nav = new Array<Nav | null>(n).fill(null)
    this.liteBytes = new Uint8Array(SL32.SIZE * n)
    this.fullBytes = new Uint8Array(DS64.SIZE * n)
    if (n === 1) {
      const start = o.start ?? [0, 0, ground ? 0 : 120]
      this.pos[0] = start[0]
      this.pos[1] = start[1]
      this.pos[2] = ground ? 0 : start[2]
    } else if (ground) {
      for (let a = 0; a < n; a++) {
        const r = 50 + (a % 10) * 20
        const ang = (2 * Math.PI * a) / n
        this.pos[3 * a] = r * Math.cos(ang)
        this.pos[3 * a + 1] = r * Math.sin(ang)
      }
    }
    // channels (ids stable for the world lifetime)
    this.rosterCh = this.addCh(1, 'fleet/roster', 'roster', 'msgpack', 'awr.fleet.roster.v1', 0, true, 0, 10, null, -1)
    this.swarmCh = this.addCh(2, 'swarm/uav/state', 'state', 'raw', 'awr.SwarmLite32.v1', 1, false, 125, 10, null, -1)
    this.envCh = this.addCh(3, 'env/state', 'state', 'msgpack', 'awr.env.keyframe.v1', 2, true, 1, 10, null, -1)
    this.addCh(4, 'event', 'event', 'json', 'awr.event.v1', 0, false, 0, 0, null, -1)
    this.perfCh = this.addCh(5, 'perf/server', 'state', 'msgpack', 'awr.perf.server.v1', 3, true, 1, 1, null, -1)
    this.procsCh = this.addCh(6, 'sys/procs', 'state', 'msgpack', 'awr.sys.procs.v1', 3, true, 1, 1, null, -1)
    for (let a = 0; a < n; a++) {
      const ent = { kind: 'uav', id: this.ids[a] }
      this.fullCh.push(this.addCh(16 + a, `uav/${this.ids[a]}/state`, 'state', 'raw', 'awr.DroneState64.v1', 0, false, 125, 30, ent, a))
      this.extCh.push(this.addCh(16 + n + a, `uav/${this.ids[a]}/state_ext`, 'state', 'msgpack', 'awr.uav.state_ext.v1', 2, true, 2, 2, ent, a))
    }
    this.step(0)
  }

  private addCh(id: number, topic: string, kind: Channel['kind'], encoding: Channel['encoding'], schemaName: string, priority: number,
    selfContained: boolean, nativeHz: number, defaultRate: number, entity: Channel['entity'], row: number): Channel {
    const c: Channel = { id, topic, kind, encoding, schemaName, priority, selfContained, nativeHz, defaultRate, entity, row, seq: 0,
      payload: null, tSimNs: 0, refs: 0 }
    this.channels.push(c)
    this.byId.set(id, c)
    return c
  }

  // ------------------------------------------------------------ connections
  attach(c: FakeSource): string {
    this.conns.add(c)
    if (this.autoTick && !this.timer) this.timer = setInterval(() => this.tick(), 1000 / TICK_HZ)
    return `c-fake-${++this.connSeq}`
  }
  detach(c: FakeSource): void {
    this.conns.delete(c)
    if (this.conns.size === 0 && this.timer) {
      clearInterval(this.timer)
      this.timer = null
    }
  }
  srvNs(): number {
    return Math.round((this.now() - this.origin) * 1e6)
  }
  indexOf(id: string): number {
    return this.ids.indexOf(id)
  }
  positionOf(a: number): [number, number, number] {
    return [this.pos[3 * a], this.pos[3 * a + 1], this.pos[3 * a + 2]]
  }

  // ------------------------------------------------------------ test hooks
  /** scenario-reset stand-in: global epoch + 1; every connection gets TIME first, then SNAPSHOT (AWR-17 §6.10) */
  bumpEpoch(): void {
    this.time.epoch = (this.time.epoch + 1) & 0xffff
    for (const c of this.conns) c.onEpoch()
  }
  /** api restart stand-in: a new sessionId and event sequence; open connections close with 1001 */
  restart(sessionId?: string): void {
    this.stats.restarts++
    this.sessionId = sessionId ?? `fake-${this.seed}-r${this.stats.restarts}`
    this.gseq = 0
    this.ring.length = 0
    this.closeAll(1001)
  }
  closeAll(code: number): void {
    for (const c of [...this.conns]) c.serverClose(code)
  }
  /** emit k events now (event storm, hidden-page tests) */
  emitStorm(k: number, type = 'sim.vehicle.state'): void {
    for (let i = 0; i < k; i++) this.emit(type, 0, { i }, this.ids[i % this.n], null)
  }
  setStatus(id: string, level: 'info' | 'warning' | 'error', message: string): void {
    const m = { op: 'status', id, level, message, source: 'fake' }
    this.statuses.set(id, m)
    for (const c of this.conns) c.sendCtrlIfReady(m)
  }
  removeStatus(id: string): void {
    if (!this.statuses.delete(id)) return
    for (const c of this.conns) c.sendCtrlIfReady({ op: 'removeStatus', ids: [id] })
  }

  // ------------------------------------------------------------ events
  emit(type: string, level: number, data: Record<string, unknown>, uav: string | null, cid: string | null): void {
    const e: FakeEvent = { seq: ++this.gseq, t_sim_ns: this.tSimNs, t_wall_ns: String(Math.round(this.now() * 1e6)), type, level,
      producer: 'sim-core', uav, cid, data }
    this.ring.push(e)
    if (this.ring.length > EVENT_RING) this.ring.shift()
    for (const c of this.conns) c.queueEvent(e)
  }

  // ------------------------------------------------------------ calls and results
  /** record and route a result to the connection that owns the call (re-bound on a duplicate re-send) */
  result(id: string, status: string, code: number, extra: Record<string, unknown> = {}): void {
    const m: Record<string, unknown> = { op: 'result', id, status, code, ...extra }
    if (code !== 0 && m.reason === undefined) m.reason = reasonName(code)
    const rec = this.results.get(id)
    const fin = m.final === true
    if (rec) {
      rec.last = m
      rec.final = fin
    } else this.results.set(id, { last: m, final: fin, conn: null })
    const conn = this.results.get(id)!.conn
    conn?.sendCtrlIfReady(m)
  }
  private reject(id: string, code: number, message: string, uav: string | null, op: string): void {
    this.result(id, 'rejected', code, { message, final: true, effect: { status: 'UNAVAILABLE', verify_trust: 0 } })
    this.emit('cmd.rejected', 1, { op, code }, uav, id)
  }
  private accept(id: string, op: string): void {
    this.result(id, 'accepted', 0, { effect: { status: 'UNVERIFIED', verify_trust: 1, native_ack: true, protocol: 'inproc', requested: op } })
  }
  private succeedNow(id: string, op: string, uav: string | null): void {
    this.accept(id, op)
    this.result(id, 'succeeded', 0, { final: true, effect: { status: 'OK', verify_trust: 4, simulated: true, metrics: {}, latency_ms: 0 } })
    this.emit('cmd.succeeded', 0, { op, code: 0 }, uav, id)
  }

  handleCall(conn: FakeSource, m: Record<string, unknown>): void {
    this.stats.calls++
    const id = str(m.id)
    const service = str(m.service)
    const args = (m.args ?? {}) as Record<string, unknown>
    if (!id) {
      conn.error(Reason.BAD_REQUEST, 'call without id', 'call')
      return
    }
    const known = this.results.get(id)
    if (known) {
      // the same call id re-sent (reconnect): no second execution, the latest result with duplicate (AWR-17 §7.5 item 1)
      this.stats.duplicates++
      known.conn = conn
      conn.sendCtrlIfReady({ ...known.last, duplicate: true })
      return
    }
    this.results.set(id, { last: {}, final: false, conn })
    if (this.results.size > RESULT_TABLE_CAP) this.results.delete(this.results.keys().next().value!)
    const ms = matchService(service)
    if (!ms) {
      this.reject(id, Reason.PARAM_OUT_OF_RANGE, `unknown service ${service}`, null, 'unknown')
      return
    }
    const op = ms.service.op
    if (op === 'fleet/cmd') {
      this.handleBatch(id, ms.params.op ?? '', args)
      return
    }
    const uav = ms.params.id ?? null
    const a = uav ? this.ids.indexOf(uav) : -1
    if (uav !== null && a < 0) {
      this.reject(id, Reason.NO_VEHICLE, `no vehicle ${uav}`, uav, op)
      return
    }
    if (a < 0) {
      // sim, env, seat services: accept and succeed (the fake has no clock or environment control)
      this.succeedNow(id, op, null)
      return
    }
    const code = this.admit(op, a)
    if (code !== 0) {
      this.reject(id, code, REASONS[code]?.message_zh ?? 'rejected', uav, op)
      return
    }
    this.startOp(a, op, args, id, null, conn)
  }

  /** admission stand-in (AWR-12 §5.2 matrix); 0 when accepted */
  private admit(op: string, a: number): number {
    try {
      const [sym] = admitSymbol(op, this.fs[a] & 0x1f, this.flags[a])
      if (sym === '-') return Reason.STATE
      if (sym === 'S') return Reason.SAFETY_ACTIVE
      if (sym === '=' && op !== 'goto') return Reason.DUPLICATE
    } catch {
      // op without an admission row: accepted
    }
    return 0
  }

  /** start an admitted command on vehicle a; batch children report through the aggregate only */
  private startOp(a: number, op: string, args: Record<string, unknown>, id: string, batch: BatchAgg | null, conn: FakeSource | null): void {
    const uav = this.ids[a]
    const o = 3 * a
    const ack = (): void => {
      if (batch) batch.counts.accepted++
      else {
        this.accept(id, op)
        this.emit('cmd.accepted', 0, { op, code: 0 }, uav, id)
      }
    }
    const done = (): void => {
      if (batch) {
        batch.counts.succeeded++
        batch.dirty = true
      } else this.succeedNow(id, op, uav)
    }
    switch (op) {
      case 'goto': {
        const p = args.pos as unknown
        if (!Array.isArray(p) || p.length !== 3 || !p.every((x) => typeof x === 'number' && Number.isFinite(x))) {
          this.reject(id, Reason.PARAM_OUT_OF_RANGE, 'goto.pos must be [3] finite numbers (m)', uav, op)
          return
        }
        const speed = typeof args.speed_mps === 'number' && args.speed_mps > 0 ? Math.min(args.speed_mps, 12) : GOTO_DEFAULT_MPS
        const tol = typeof args.tol_m === 'number' ? Math.min(10, Math.max(0.2, args.tol_m)) : GOTO_TOL_M
        this.supersede(a)
        this.nav[a] = this.newNav(id, 'goto', Float64Array.from(p as number[]), speed, tol, batch, conn)
        this.mode[a] = MODE_NAV
        this.fs[a] = packFs(FlightState.FLYING, SUB_GOTO)
        const t = this.nav[a]!.target
        this.result(id, 'accepted', 0, { effect: { status: 'UNVERIFIED', verify_trust: 1, native_ack: true, protocol: 'inproc',
          requested: `goto enu=(${round3(t[0])},${round3(t[1])},${round3(t[2])}) v=${speed}` } })
        this.emit('cmd.accepted', 0, { op, code: 0 }, uav, id)
        return
      }
      case 'takeoff': {
        const alt = typeof args.alt_m === 'number' ? Math.min(120, Math.max(0.5, args.alt_m)) : 2.5
        this.supersede(a)
        this.nav[a] = this.newNav(id, 'takeoff', Float64Array.of(this.pos[o], this.pos[o + 1], alt), CLIMB_MPS, 0.2, batch, conn)
        this.mode[a] = MODE_NAV
        this.fs[a] = packFs(FlightState.TAKING_OFF)
        this.flags[a] = FLAGS_AIR
        ack()
        return
      }
      case 'land': {
        this.supersede(a)
        this.nav[a] = this.newNav(id, 'land', Float64Array.of(this.pos[o], this.pos[o + 1], 0), DESCENT_MPS, GROUND_EPS_M, batch, conn)
        this.mode[a] = MODE_NAV
        this.fs[a] = packFs(FlightState.LANDING)
        ack()
        return
      }
      case 'rtl': {
        this.supersede(a)
        const nav = this.newNav(id, 'rtl', Float64Array.of(0, 0, Math.max(this.pos[o + 2], 10)), GOTO_DEFAULT_MPS, 1, batch, conn)
        nav.land = args.land !== false
        this.nav[a] = nav
        this.mode[a] = MODE_NAV
        this.fs[a] = packFs(FlightState.RTL)
        ack()
        return
      }
      case 'velocity': {
        this.supersede(a)
        const nav = this.newNav(id, 'velocity', Float64Array.of(0, 0, 0), 0, 0, null, conn)
        nav.vmax = typeof args.vmax_mps === 'number' && args.vmax_mps > 0 ? args.vmax_mps : 12
        nav.body = args.frame === 'body'
        nav.holdAlt = args.hold_alt !== false
        nav.lastSetpointAt = this.now()
        this.nav[a] = nav
        this.mode[a] = MODE_VELOCITY
        this.vel.fill(0, o, o + 3)
        this.accept(id, op)
        this.emit('cmd.accepted', 0, { op, code: 0 }, uav, id)
        return
      }
      case 'velocity_stop': {
        const v = this.nav[a]
        if (v?.kind === 'velocity') {
          this.nav[a] = null
          this.result(v.id, 'succeeded', 0, { final: true, effect: { status: 'OK', verify_trust: 4, simulated: true, metrics: { t_exec_s: round3((this.tSimNs - v.t0Sim) / 1e9) }, latency_ms: 0 } })
        }
        this.hover(a)
        done()
        return
      }
      case 'hover':
      case 'pause':
      case 'safety_stop':
        this.supersede(a)
        this.hover(a)
        done()
        return
      default:
        done()
    }
  }

  private newNav(id: string, kind: NavKind, target: Float64Array, speed: number, tol: number, batch: BatchAgg | null, conn: FakeSource | null): Nav {
    return { id, kind, target, speed, tol, t0Sim: this.tSimNs, stage: 0, lastProgressTick: this.k, batch, land: false, vmax: 0, body: false,
      holdAlt: true, lastSetpointAt: 0, conn }
  }

  private hover(a: number): void {
    const o = 3 * a
    this.mode[a] = MODE_HOVER
    this.vel.fill(0, o, o + 3)
    this.yawRate[a] = 0
    if ((this.flags[a] & FlightFlags.IN_AIR) !== 0) this.fs[a] = packFs(FlightState.FLYING, SUB_HOVER)
  }

  private supersede(a: number): void {
    const prev = this.nav[a]
    if (!prev) return
    this.nav[a] = null
    if (prev.batch) {
      const c = prev.batch.counts
      if (prev.stage === 0) c.accepted--
      else c.running--
      c.canceled++
      prev.batch.dirty = true
      return
    }
    this.result(prev.id, 'canceled', Reason.SUPERSEDED, { message: 'superseded', final: true,
      effect: { status: 'UNVERIFIED', verify_trust: prev.stage === 1 ? 2 : 1, message: 'superseded' } })
  }

  cancelCall(conn: FakeSource, id: string): void {
    const a = this.nav.findIndex((c) => c?.id === id)
    if (a < 0) {
      conn.sendCtrlIfReady({ op: 'result', id, status: 'rejected', code: Reason.STATE, reason: 'STATE', message: 'call already final', final: true })
      return
    }
    this.nav[a] = null
    this.hover(a)
    this.result(id, 'canceled', Reason.CANCELLED, { message: 'canceled', final: true, effect: { status: 'UNVERIFIED', verify_trust: 2 } })
  }

  /** fleet/cmd/{op}: summary result, counts in progress and fleet.batch.progress, final when all children are terminal */
  private handleBatch(id: string, op: string, args: Record<string, unknown>): void {
    const vehicles = args.vehicles
    if (!BATCH_OPS.includes(op as BatchOp)) {
      this.reject(id, Reason.PARAM_OUT_OF_RANGE, `fleet/cmd does not accept ${op}`, null, `fleet/cmd/${op}`)
      return
    }
    const list = vehicles === '*' ? this.ids : Array.isArray(vehicles) ? vehicles.map((v) => String(v)) : null
    if (!list || list.length === 0 || list.length > 1000) {
      this.reject(id, Reason.PARAM_OUT_OF_RANGE, 'vehicles must be 1..1000 ids or "*"', null, `fleet/cmd/${op}`)
      return
    }
    const sub = (args.args ?? {}) as Record<string, unknown>
    const agg: BatchAgg = { id, op, counts: newCounts(), lastProgressK: this.k, dirty: true, done: false }
    const accepted: string[] = []
    const rejected: [string, number][] = []
    const byCode: Record<string, number> = {}
    for (const vid of list) {
      const a = this.ids.indexOf(vid)
      const code = a < 0 ? Reason.NO_VEHICLE : this.admit(op, a)
      if (code !== 0) {
        rejected.push([vid, code])
        byCode[String(code)] = (byCode[String(code)] ?? 0) + 1
        agg.counts.rejected++
        continue
      }
      accepted.push(vid)
      this.startOp(a, op, sub, `${id}:${vid}`, agg, null)
    }
    const data = { accepted_n: accepted.length, rejected_n: rejected.length, rejected_by_code: byCode, accepted, rejected }
    if (accepted.length === 0) {
      const top = Number(Object.entries(byCode).sort((x, y) => y[1] - x[1])[0]?.[0] ?? Reason.STATE)
      this.result(id, 'rejected', top, { message: 'no vehicle accepted the batch', final: true, data, effect: { status: 'UNAVAILABLE', verify_trust: 0 } })
      return
    }
    this.result(id, 'accepted', 0, { data, effect: { status: 'UNVERIFIED', verify_trust: 1, native_ack: true, protocol: 'inproc', requested: `fleet/cmd/${op}` } })
    this.batches.push(agg)
  }

  private batchUpdates(k: number): void {
    for (let i = this.batches.length - 1; i >= 0; i--) {
      const b = this.batches[i]
      const open = b.counts.accepted + b.counts.running
      if (b.dirty && (k - b.lastProgressK >= PROGRESS_EVERY_TICKS || open === 0)) {
        b.dirty = false
        b.lastProgressK = k
        const counts = { ...b.counts }
        this.results.get(b.id)?.conn?.sendCtrlIfReady({ op: 'progress', id: b.id, data: { phase: 'executing', counts } })
        this.emit('fleet.batch.progress', 0, { batch_id: b.id, counts }, null, b.id)
      }
      if (open === 0) {
        const ok = b.counts.failed + b.counts.canceled === 0
        this.result(b.id, ok ? 'succeeded' : 'failed', 0, { final: true, data: { counts: { ...b.counts } },
          effect: ok ? { status: 'OK', verify_trust: 4, simulated: true, metrics: { n: b.counts.succeeded }, latency_ms: 0 } : { status: 'FAILED', verify_trust: 2 } })
        this.batches.splice(i, 1)
      }
    }
  }

  // ------------------------------------------------------------ teleoperation (CLIENT_DATA)
  /** a setpoint for vehicle a from conn; false when that connection has no running velocity call (322) */
  applySetpoint(conn: FakeSource, a: number, vx: number, vy: number, vz: number, yawRate: number, final: boolean): boolean {
    const nav = this.nav[a]
    if (!nav || nav.kind !== 'velocity' || nav.conn !== conn) return false
    this.stats.setpoints++
    const o = 3 * a
    let ex = Number.isFinite(vx) ? vx : 0
    let ny = Number.isFinite(vy) ? vy : 0
    if (nav.body) {
      const c = Math.cos(this.yaw[a])
      const s = Math.sin(this.yaw[a])
      const x = ex
      ex = c * x - s * ny
      ny = s * x + c * ny
    }
    const up = nav.holdAlt || !Number.isFinite(vz) ? 0 : vz
    const sp = Math.hypot(ex, ny, up)
    const k = sp > nav.vmax ? nav.vmax / sp : 1
    this.vel[o] = ex * k
    this.vel[o + 1] = ny * k
    this.vel[o + 2] = up * k
    this.yawRate[a] = Number.isFinite(yawRate) ? yawRate : 0
    nav.lastSetpointAt = this.now()
    if (nav.stage === 0) {
      nav.stage = 1
      this.result(nav.id, 'running', 0, { effect: { status: 'UNVERIFIED', verify_trust: 2, observed_state: 'FLYING/VELOCITY' } })
    }
    if (final) {
      this.vel.fill(0, o, o + 3)
      this.yawRate[a] = 0
    }
    return true
  }

  // ------------------------------------------------------------ simulation
  /** advance the vehicles to t (s); writes Lite32 and Full64 rows with the generated offsets */
  private step(tS: number): void {
    const n = this.n
    const dt = Math.max(0, tS - this.lastStepS)
    this.lastStepS = tS
    for (let a = 0; a < n; a++) {
      const o = 3 * a
      const m = this.mode[a]
      if (m === MODE_ORBIT) {
        const r = 50 + (a % 10) * 20
        const om = 8 / r
        const ang = (2 * Math.PI * a) / n + om * tS
        const c = Math.cos(ang)
        const s = Math.sin(ang)
        this.pos[o] = r * c
        this.pos[o + 1] = r * s
        this.pos[o + 2] = 30 + (a % 7) * 5 + 2 * Math.sin(0.2 * tS + a)
        this.vel[o] = -r * om * s
        this.vel[o + 1] = r * om * c
        this.vel[o + 2] = 0.4 * Math.cos(0.2 * tS + a)
        this.yaw[a] = ang + Math.PI / 2
      } else if (m === MODE_NAV) {
        this.stepNav(a, dt)
      } else if (m === MODE_VELOCITY) {
        this.pos[o] += this.vel[o] * dt
        this.pos[o + 1] += this.vel[o + 1] * dt
        this.pos[o + 2] = Math.max(0.5, this.pos[o + 2] + this.vel[o + 2] * dt)
        this.yaw[a] += this.yawRate[a] * dt
      } else {
        this.vel[o] = 0
        this.vel[o + 1] = 0
        this.vel[o + 2] = 0
      }
      this.battery[a] = Math.max(0, Math.min(100, 100 - (a % 40) - Math.floor(tS / 60)))
    }
    this.writeRows()
  }

  private stepNav(a: number, dt: number): void {
    const c = this.nav[a]
    const o = 3 * a
    if (!c) {
      this.mode[a] = MODE_HOVER
      return
    }
    const dx = c.target[0] - this.pos[o]
    const dy = c.target[1] - this.pos[o + 1]
    const dz = c.target[2] - this.pos[o + 2]
    const d = Math.hypot(dx, dy, dz)
    const v = c.kind === 'goto' || c.kind === 'rtl' ? Math.min(c.speed, 1.2 * d) : Math.min(c.speed, 2 * d + 0.2)
    if (d > 1e-9) {
      const k = Math.min(1, (v * dt) / d)
      this.pos[o] += dx * k
      this.pos[o + 1] += dy * k
      this.pos[o + 2] += dz * k
      this.vel[o] = (dx / d) * v
      this.vel[o + 1] = (dy / d) * v
      this.vel[o + 2] = (dz / d) * v
      if (Math.hypot(dx, dy) > 0.5) this.yaw[a] = Math.atan2(dy, dx)
    }
  }

  private navUpdates(k: number): void {
    const now = this.now()
    for (let a = 0; a < this.n; a++) {
      const c = this.nav[a]
      if (!c) continue
      const o = 3 * a
      const uav = this.ids[a]
      if (c.kind === 'velocity') {
        if (now - c.lastSetpointAt > VELOCITY_WATCHDOG_MS) {
          this.nav[a] = null
          this.hover(a)
          this.result(c.id, 'canceled', Reason.WATCHDOG, { message: REASONS[Reason.WATCHDOG].message_zh, final: true,
            effect: { status: 'UNVERIFIED', verify_trust: c.stage === 1 ? 2 : 1 } })
          this.emit('cmd.canceled', 1, { op: 'velocity', code: Reason.WATCHDOG }, uav, c.id)
        }
        continue
      }
      const d = Math.hypot(c.target[0] - this.pos[o], c.target[1] - this.pos[o + 1], c.target[2] - this.pos[o + 2])
      if (c.stage === 0) {
        c.stage = 1
        if (c.batch) {
          c.batch.counts.accepted--
          c.batch.counts.running++
          c.batch.dirty = true
        } else {
          this.result(c.id, 'running', 0, { effect: { status: 'UNVERIFIED', verify_trust: 2, observed_state: FLIGHT_OBS[c.kind] } })
          this.emit('cmd.running', 0, { op: c.kind, code: 0 }, uav, c.id)
        }
      }
      if (d > c.tol) {
        if (!c.batch && k - c.lastProgressTick >= PROGRESS_EVERY_TICKS) {
          c.lastProgressTick = k
          const v = Math.max(0.1, Math.min(c.speed, 1.2 * d))
          this.results.get(c.id)?.conn?.sendCtrlIfReady({ op: 'progress', id: c.id, data: { phase: 'executing', dist_m: round3(d), eta_s: round3(d / v) } })
        }
        continue
      }
      if (c.kind === 'rtl' && c.land && c.target[2] > 0) {
        // arrived above home: descend (LANDING) before the call completes
        c.target[2] = 0
        c.tol = GROUND_EPS_M
        c.speed = DESCENT_MPS
        this.fs[a] = packFs(FlightState.LANDING)
        continue
      }
      this.nav[a] = null
      this.vel.fill(0, o, o + 3)
      if (c.kind === 'land' || c.kind === 'rtl' && c.land) {
        this.pos[o + 2] = 0
        this.mode[a] = MODE_HOVER
        this.fs[a] = packFs(FlightState.LANDED)
        this.flags[a] = FLAGS_GROUND
      } else this.hover(a)
      if (c.batch) {
        c.batch.counts.running--
        c.batch.counts.succeeded++
        c.batch.dirty = true
        continue
      }
      const tExec = (this.tSimNs - c.t0Sim) / 1e9
      this.result(c.id, 'succeeded', 0, { final: true, effect: { status: 'OK', verify_trust: 4, simulated: true,
        metrics: { dist_err_m: round3(d), t_exec_s: round3(tExec) }, latency_ms: Math.round(tExec * 1000) } })
      this.emit('cmd.succeeded', 0, { op: c.kind, code: 0 }, uav, c.id)
    }
  }

  private writeRows(): void {
    const n = this.n
    const lite = new DataView(this.liteBytes.buffer)
    const full = new DataView(this.fullBytes.buffer)
    const ctrlOp = packCtrl(Owner.OPERATOR, false, 2, PoseSrc.TRUTH)
    const ctrlMis = packCtrl(Owner.MISSION, false, 2, PoseSrc.TRUTH)
    for (let a = 0; a < n; a++) {
      const o = 3 * a
      const qz = Math.sin(this.yaw[a] / 2)
      const qw = Math.cos(this.yaw[a] / 2)
      const ctrl = n === 1 || this.mode[a] !== MODE_ORBIT ? ctrlOp : ctrlMis
      const fl = this.flags[a]
      const b = SL32.SIZE * a
      lite.setUint16(b + SL32.AGENT_NO, a, true)
      lite.setUint8(b + SL32.FLIGHT_STATE, this.fs[a])
      lite.setUint8(b + SL32.BATTERY_PCT, this.battery[a])
      lite.setFloat32(b + SL32.POS, this.pos[o], true)
      lite.setFloat32(b + SL32.POS + 4, this.pos[o + 1], true)
      lite.setFloat32(b + SL32.POS + 8, this.pos[o + 2], true)
      lite.setInt16(b + SL32.Q_SNORM, 0, true)
      lite.setInt16(b + SL32.Q_SNORM + 2, 0, true)
      lite.setInt16(b + SL32.Q_SNORM + 4, clampI16(qz * 32767), true)
      lite.setInt16(b + SL32.Q_SNORM + 6, clampI16(qw * 32767), true)
      lite.setInt16(b + SL32.VEL_CMS, clampI16(this.vel[o] * 100), true)
      lite.setInt16(b + SL32.VEL_CMS + 2, clampI16(this.vel[o + 1] * 100), true)
      lite.setInt16(b + SL32.VEL_CMS + 4, clampI16(this.vel[o + 2] * 100), true)
      lite.setUint8(b + SL32.FLAGS, fl)
      lite.setUint8(b + SL32.CTRL, ctrl)
      const f = DS64.SIZE * a
      full.setUint16(f + DS64.AGENT_NO, a, true)
      full.setUint8(f + DS64.FLIGHT_STATE, this.fs[a])
      full.setUint8(f + DS64.FLAGS, fl)
      full.setUint16(f + DS64.MISSION_ITEM, 0xffff, true)
      full.setUint8(f + DS64.BATTERY_PCT, this.battery[a])
      full.setUint8(f + DS64.CTRL, ctrl)
      for (let j = 0; j < 3; j++) {
        full.setFloat32(f + DS64.POS + 4 * j, this.pos[o + j], true)
        full.setFloat32(f + DS64.VEL + 4 * j, this.vel[o + j], true)
        full.setFloat32(f + DS64.OMEGA + 4 * j, j === 2 ? this.yawRate[a] : 0, true)
      }
      full.setFloat32(f + DS64.Q, 0, true)
      full.setFloat32(f + DS64.Q + 4, 0, true)
      full.setFloat32(f + DS64.Q + 8, qz, true)
      full.setFloat32(f + DS64.Q + 12, qw, true)
      full.setInt32(f + DS64.DT_US, 0, true)
    }
  }

  private roster(version: number): Record<string, unknown> {
    return { roster_version: version, entries: this.ids.map((id, a) => ({ agent_no: a, id, kind: 'uav', model: 'p600', profile_id: 'p600_mid360',
      backend: 'mock', simulated: true, producer: 'sim-core', lifecycle: 'READY', sensors: [], t_world_local: null, caps_ref: 'mock' })) }
  }
  private stateExt(a: number): Record<string, unknown> {
    return { lifecycle: 'READY', lease: { owner: this.n === 1 ? 'OPERATOR' : 'MISSION', holder: null, priority: 2, ttl_ms: null },
      loc: { status: 'TRACKING', gnss_fix: 3, sats: 14 },
      battery: { voltage_v: 23.1, current_a: 18, soc_pct: this.battery[a], t_remain_s: 1200, wh_used: 10 },
      mission: this.n === 1 ? null : { mid: 'm-fake', state: 'RUNNING', item: 1, total: 4 },
      home_enu_m: [0, 0, 0], link: { gcs_age_ms: 100, fcu_age_ms: 10 }, gcs_loss_policy: 'ignore' }
  }
  keyframe(version: number, tNs: number, from: string | null, to: string): Record<string, unknown> {
    const cfg = { ...presets().defaults.config, presets_sha256: PRESETS_SHA256 }
    const dur = 30e9
    return { schema: 'awr.env.keyframe.v1', world_id: this.worldId, version, epoch: 1, seed: this.seed, t_ns: tNs, t_apply_ns: tNs, config: cfg,
      mode: from ? 'smooth' : 'step', t0_ns: tNs, t1_ns: from ? tNs + dur : tNs, from: presetVector(from ?? to), to: presetVector(to), via: [],
      to_preset: to, anchors: { t_ns: tNs, s_m: 0, d_enu_m: [0, 0, 0], fall_rain_m: 0, fall_snow_m: 0, wetness: 0, puddle: 0 }, events: [],
      vis: { streamlines: null, vmax_mps: 20 } }
  }

  private publish(c: Channel, payload: Uint8Array): void {
    c.payload = payload
    c.seq = (c.seq + 1) >>> 0 || 1
    c.tSimNs = this.tSimNs
  }

  // ------------------------------------------------------------ tick (60 Hz aligned grid)
  tick(): void {
    this.stats.ticks++
    const k = ++this.k
    const now = this.now()
    this.tSimNs = Math.round((now - this.origin) * this.rate * 1e6)
    const tS = this.tSimNs / 1e9
    this.step(tS)
    if (k === 1) this.publish(this.rosterCh, mpEncode(this.roster(1)))
    this.publish(this.swarmCh, this.liteBytes) // copied into each frame by assemble() in this same tick
    for (const c of this.fullCh) if (c.refs > 0) this.publish(c, this.fullBytes.subarray(DS64.SIZE * c.row, DS64.SIZE * (c.row + 1)))
    if (k % TICK_HZ === 1 || k === 1) for (const c of this.extCh) if (c.refs > 0) this.publish(c, mpEncode(this.stateExt(c.row)))
    const envDue = this.envT0Ns < 0 || (this.envPeriodS > 0 && this.envVersion <= this.envCycles * this.envPresets.length
      && this.tSimNs >= this.envT0Ns + this.envPeriodS * 1e9)
    if (envDue) {
      this.envVersion++
      const from = this.envT0Ns < 0 ? null : this.envPreset
      const ids = this.envPresets
      this.envPreset = ids[(this.envVersion - 1) % ids.length]
      this.envT0Ns = this.tSimNs
      this.publish(this.envCh, mpEncode(this.keyframe(this.envVersion, this.tSimNs, this.envStep ? null : from, this.envPreset)))
      if (this.envVersion > 1) this.emit('env.changed', 1, { version: this.envVersion, by: 'fake', reason: 'preset' }, null, null)
    } else if (k % TICK_HZ === 0) {
      this.publish(this.envCh, this.envCh.payload ?? new Uint8Array(0))
    }
    if (k % TICK_HZ === 0) {
      const clients = [...this.conns].map((c) => ({ conn_id: c.connId, window: c.window, credit_skips: c.stats.creditSkips }))
      this.publish(this.perfCh, mpEncode({ api: { cpu_pct: 0, tick_overruns: 0, encodes_per_s: 0, n_clients: this.conns.size },
        clients, sim: { rtf: this.rate, n_active: this.n, kernel: 'numpy' } }))
      this.publish(this.procsCh, mpEncode({ items: [{ name: 'sim-core', pid: null, state: 'RUNNING', restarts: 0 }, { name: 'api', pid: null, state: 'RUNNING', restarts: 0 }] }))
    }
    if (k % this.eventPeriodTicks === 0) {
      const a = Math.floor(k / TICK_HZ) % this.n
      this.emit('mission.item_reached', 0, { mid: 'm-fake', item: Math.floor(k / TICK_HZ) % 4 }, this.ids[a], null)
    }
    this.navUpdates(k)
    this.batchUpdates(k)
    for (const c of [...this.conns]) c.onWorldTick(k)
    if (this.dropEveryS > 0 && now - this.lastDropAt >= this.dropEveryS * 1000) {
      this.lastDropAt = now
      this.closeAll(1001)
    }
  }
}

const FLIGHT_OBS: Readonly<Record<NavKind, string>> = {
  goto: 'FLYING/GOTO', takeoff: 'TAKING_OFF', land: 'LANDING', rtl: 'RTL', velocity: 'FLYING/VELOCITY',
}

// ================================================================ FakeSource (one connection)
/** Socket-like fake gateway connection; see the file header. */
export class FakeSource implements SocketLike {
  binaryType = 'arraybuffer'
  readyState: number = WS_CONNECTING
  onopen: ((ev?: unknown) => void) | null = null
  onmessage: ((ev: SocketMessage) => void) | null = null
  onclose: ((ev: SocketClose) => void) | null = null
  onerror: ((ev?: unknown) => void) | null = null

  /** null in replay mode */
  readonly world: FakeWorld | null
  readonly n: number
  readonly ids: string[]
  readonly window: number
  readonly stats = { frames: 0, creditSkips: 0, ticks: 0, calls: 0, pings: 0, acks: 0, setpoints: 0, clientDataDropped: 0, clientStats: 0, playback: 0 }
  connId = ''
  /** client control messages received (tests; the last 4096) */
  readonly received: Record<string, unknown>[] = []
  private readonly now: () => number
  private timer: ReturnType<typeof setInterval> | null = null
  private outbox: Array<string | ArrayBuffer> = []
  private flushQueued = false
  private hello = false
  private sentSeq = 0
  private acked = 0
  private eventGap = false
  // subscriptions
  private readonly subs = new Map<number, { topic: string; channels: Channel[]; rate: number }>()
  private readonly sched = new Map<number, SubState>()
  private active: Channel[] = []
  private eventsOn = false
  private eventFilter: { types: readonly string[]; levelMin: number } | null = null
  private resumeFrom = -1
  private pendingEvents: FakeEvent[] = []
  // client publish channels (C->S advertise): id -> vehicle
  private readonly clientCh = new Map<number, { vehicle: string; lastSeq: number; denied: boolean }>()
  // replay state
  private readonly replay: AwrtRecord[] | null
  private replayIdx = 0
  private replayBase = 0
  private replayLoopFrom = 0

  constructor(o: FakeSourceOptions = {}) {
    if (o.fixture) {
      this.world = null
      this.now = o.now ?? (() => performance.now())
      this.n = 0
      this.ids = []
      this.window = 6
      this.replay = readAwrrt(o.fixture instanceof Uint8Array ? o.fixture : new Uint8Array(o.fixture)).records.filter((r) => r.dir === AWRT_S2C)
      const firstBin = this.replay.findIndex((r) => r.kind !== AWRT_TEXT)
      this.replayLoopFrom = o.loop ? Math.max(0, firstBin) : -1
      if (o.autoTick !== false) this.timer = setInterval(() => this.tick(), 1000 / TICK_HZ)
    } else {
      this.world = o.shared ?? new FakeWorld(o)
      this.now = this.world.now
      this.n = this.world.n
      this.ids = this.world.ids
      this.window = Math.min(8, Math.max(3, o.window ?? this.world.window))
      this.replay = null
    }
    queueMicrotask(() => this.open())
  }

  // ------------------------------------------------------------ socket API
  send(data: string | ArrayBuffer | ArrayBufferView): void {
    if (this.readyState !== WS_OPEN) throw new Error('FakeSource: send on a closed socket')
    if (this.replay) return // replay ignores the client
    if (typeof data !== 'string') {
      this.onClientData(data instanceof ArrayBuffer ? new DataView(data) : new DataView(data.buffer, data.byteOffset, data.byteLength))
      return
    }
    let m: Record<string, unknown>
    try {
      m = JSON.parse(data) as Record<string, unknown>
    } catch {
      this.error(Reason.BAD_REQUEST, 'control messages must be JSON with op', 'unknown')
      return
    }
    this.received.push(m)
    if (this.received.length > 4096) this.received.shift()
    this.onText(m)
  }

  close(code = 1000, reason = ''): void {
    if (this.readyState === WS_CLOSED) return
    this.readyState = WS_CLOSED
    if (this.timer) clearInterval(this.timer)
    this.timer = null
    this.world?.detach(this)
    queueMicrotask(() => this.onclose?.({ code, reason }))
  }

  /** test hook: server-side close with a code (for reconnect tests) */
  serverClose(code: number): void {
    this.close(code, 'server')
  }

  /** advance one 60 Hz tick (drives the shared world, or the replay) */
  tick(): void {
    if (this.readyState !== WS_OPEN) return
    this.stats.ticks++
    if (this.replay) this.tickReplay()
    else this.world!.tick()
  }

  // ------------------------------------------------------------ plumbing
  private emitOut(m: string | ArrayBuffer): void {
    this.outbox.push(m)
    if (!this.flushQueued) {
      this.flushQueued = true
      queueMicrotask(() => this.flushOut())
    }
  }
  private flushOut(): void {
    this.flushQueued = false
    const q = this.outbox
    this.outbox = []
    for (const m of q) {
      if (this.readyState !== WS_OPEN) return
      this.onmessage?.({ data: m })
    }
  }
  private sendCtrl(m: Record<string, unknown>): void {
    this.emitOut(JSON.stringify(m))
  }
  /** control message from the world (results, progress, status) once the connection is open */
  sendCtrlIfReady(m: Record<string, unknown>): void {
    if (this.readyState === WS_OPEN) this.sendCtrl(m)
  }
  error(code: number, message: string, op: string, id?: number | string): void {
    const ref: Record<string, unknown> = { op }
    if (id !== undefined) ref.id = id
    this.sendCtrl({ op: 'error', code, name: reasonName(code), message, ref })
  }

  private open(): void {
    if (this.readyState !== WS_CONNECTING) return
    this.readyState = WS_OPEN
    this.replayBase = this.now()
    this.onopen?.()
    if (this.replay) {
      this.tickReplay()
      return
    }
    const w = this.world!
    this.connId = w.attach(this)
    this.sendCtrl(this.serverInfo())
    this.sendCtrl({ op: 'advertise', channels: w.channels.map((c) => advert(c)) })
    this.sendTime()
    for (const s of w.statuses.values()) this.sendCtrl(s)
  }

  serverInfo(): Record<string, unknown> {
    const w = this.world!
    const layouts: Record<string, string> = {}
    for (const s of INFO_SCHEMAS) layouts[s] = SCHEMA_HASH[s]
    return {
      op: 'serverInfo', name: 'awr-gateway', protocol: 'awr.rt.v1', sessionId: w.sessionId, connId: this.connId,
      capabilities: ['time', 'credit', 'rpc', 'events', 'clientPublish'], window: this.window, tickHz: TICK_HZ, rateClasses: [1, 2, 5, 10, 15, 20, 30, 60],
      world: { id: w.worldId, frame: 'world' }, run: { id: 'fake-run', segment: 0 }, mode: 'live',
      clock: { mode: 'lockstep', pausable: true, max_speed: 20, steppable: true }, role: 'operator', principal: 'p-fake', seat: 'held',
      contracts: CONTRACTS_VERSION, layouts,
      limits: { maxSubs: 256, maxHighRateFull: 64, ctrlQueue: 1024, maxTextBytes: 262144, maxBinaryBytes: 4096 },
      serverUnix_ns: String(Math.round(this.now() * 1e6)),
    }
  }

  private sendTime(): void {
    const w = this.world!
    w.time.t_sim_ns = w.tSimNs
    w.time.t_srv_ns = w.srvNs()
    const b = new ArrayBuffer(24)
    writeTime(new DataView(b), 0, w.time)
    this.emitOut(b)
  }

  /** the world changed epoch: TIME first, then every subscribed channel in the next frame as SNAPSHOT */
  onEpoch(): void {
    if (!this.hello || this.readyState !== WS_OPEN) return
    this.sendTime()
    for (const [id, st] of this.sched) if (st.refs > 0 && this.world!.byId.get(id)!.seq !== 0) st.snapshot = true
  }

  // ------------------------------------------------------------ control messages
  private onText(m: Record<string, unknown>): void {
    const op = m.op
    const w = this.world!
    if (!this.hello) {
      if (op !== 'hello') {
        this.error(Reason.BAD_REQUEST, 'only hello is accepted before hello', String(op))
        return
      }
      const major = str(m.contracts).split('.')[0]
      if (major !== CONTRACTS_VERSION.split('.')[0]) {
        this.error(Reason.PROTOCOL_UNSUPPORTED, `contracts major differs: ${String(m.contracts)}`, 'hello')
        this.close(4426, 'contracts')
        return
      }
      this.hello = true
      const r = m.resume as { sessionId?: string; lastEventSeq?: number } | undefined
      if (r && r.sessionId === w.sessionId && typeof r.lastEventSeq === 'number') this.resumeFrom = r.lastEventSeq
      return
    }
    switch (op) {
      case 'subscribe':
        this.subscribe((m.subs as Record<string, unknown>[] | undefined) ?? [])
        break
      case 'unsubscribe':
        for (const id of (m.ids as number[] | undefined) ?? []) this.unsubscribe(id)
        this.rebuildActive()
        break
      case 'ack':
        this.stats.acks++
        this.acked = Math.max(this.acked, Number(m.frame) || 0)
        break
      case 'ping':
        this.stats.pings++
        this.sendCtrl({ op: 'pong', t: Number(m.t) || 0, server_ns: w.srvNs(), sim_ns: w.tSimNs, epoch: w.time.epoch, unix_ns: String(Math.round(this.now() * 1e6)) })
        break
      case 'call':
        this.stats.calls++
        w.handleCall(this, m)
        break
      case 'cancel':
        w.cancelCall(this, str(m.id))
        break
      case 'advertise':
        for (const c of (m.channels as Record<string, unknown>[] | undefined) ?? []) {
          const id = Number(c.id)
          const topic = str(c.topic)
          const vid = topic.split('/')[1] ?? ''
          if (id >= 1 && id <= 255 && topic.endsWith('/setpoint')) this.clientCh.set(id, { vehicle: vid, lastSeq: 0, denied: false })
          else this.error(Reason.CLIENT_PUBLISH_DENIED, 'only uav/{id}/setpoint can be published', 'advertise', id)
        }
        break
      case 'unadvertise':
        for (const id of (m.ids as number[] | undefined) ?? []) this.clientCh.delete(Number(id))
        break
      case 'playback':
        this.stats.playback++
        // D1-core gateway parity: replay is D1-ext, the service is unavailable
        this.sendCtrl({ op: 'playbackState', status: 'error', code: Reason.SERVICE_UNAVAILABLE, request_id: str(m.request_id) })
        break
      case 'clientStats':
        this.stats.clientStats++
        break
      case 'hello':
        break
      default:
        this.error(Reason.UNKNOWN_OP, `unknown op ${String(op)}`, String(op))
    }
  }

  private onClientData(dv: DataView): void {
    const w = this.world!
    if (dv.byteLength < 32 || dv.getUint8(0) !== OP_CLIENT_DATA) {
      this.stats.clientDataDropped++
      return
    }
    const ch = dv.getUint16(2, true)
    const seq = dv.getUint32(4, true)
    const cc = this.clientCh.get(ch)
    if (!cc) {
      this.stats.clientDataDropped++
      this.error(Reason.CLIENT_PUBLISH_DENIED, `channel ${ch} not advertised`, 'clientData', ch)
      return
    }
    if (seq <= cc.lastSeq) {
      this.stats.clientDataDropped++
      return
    }
    cc.lastSeq = seq
    const a = w.indexOf(cc.vehicle)
    const ok = a >= 0 && w.applySetpoint(this, a, dv.getFloat32(16, true), dv.getFloat32(20, true), dv.getFloat32(24, true), dv.getFloat32(28, true),
      (dv.getUint8(1) & CD_FINAL) !== 0)
    if (!ok) {
      this.stats.clientDataDropped++
      if (!cc.denied) {
        cc.denied = true
        this.error(Reason.CLIENT_PUBLISH_DENIED, `no running velocity call for ${cc.vehicle}`, 'clientData', ch)
      }
      return
    }
    cc.denied = false
    this.stats.setpoints++
  }

  private subscribe(list: Record<string, unknown>[]): void {
    const w = this.world!
    for (const s of list) {
      const sid = Number(s.id)
      let topic = str(s.topic)
      if (topic === 'swarm/state') topic = 'swarm/uav/state'
      if (topic === 'event') {
        this.eventsOn = true
        const f = s.filter as { types?: string[]; levelMin?: number } | undefined
        this.eventFilter = f ? { types: f.types ?? [], levelMin: f.levelMin ?? 0 } : null
        this.subs.set(sid, { topic, channels: [], rate: 0 })
        this.sendCtrl({ op: 'subscribed', id: sid, topic, channels: [4], rate: 0, mode: 'all' })
        if (this.resumeFrom >= 0) this.replayEvents(this.resumeFrom)
        continue
      }
      if (!topic.split('/').every((p) => /^[a-z0-9][a-z0-9_-]*$/.test(p) || p === '*' || p === '**')) {
        this.error(Reason.TOPIC_INVALID, `bad topic ${topic}`, 'subscribe', sid)
        continue
      }
      const rate = quantizeRate(Number(s.rate) || 0)
      const prev = this.subs.get(sid)
      if (prev) this.unsubscribe(sid)
      const chans = w.channels.filter((c) => c.kind !== 'event' && keyMatches(topic, c.topic))
      this.subs.set(sid, { topic, channels: chans, rate })
      for (const c of chans) {
        let st = this.sched.get(c.id)
        if (!st) {
          st = { refs: 0, rate: 0, period: 1, lastSeq: 0, snapshot: false, pending: false }
          this.sched.set(c.id, st)
        }
        if (st.refs === 0) c.refs++
        st.refs++
        this.recomputeRate(c.id)
        if (!prev) st.snapshot = true
      }
      this.sendCtrl({ op: 'subscribed', id: sid, topic, channels: chans.map((c) => c.id), rate, mode: 'latest' })
    }
    this.rebuildActive()
  }
  private unsubscribe(sid: number): void {
    const s = this.subs.get(sid)
    if (!s) return
    this.subs.delete(sid)
    if (s.topic === 'event') this.eventsOn = [...this.subs.values()].some((x) => x.topic === 'event')
    for (const c of s.channels) {
      const st = this.sched.get(c.id)
      if (!st) continue
      st.refs = Math.max(0, st.refs - 1)
      if (st.refs === 0) c.refs = Math.max(0, c.refs - 1)
      this.recomputeRate(c.id)
    }
  }
  private recomputeRate(id: number): void {
    const st = this.sched.get(id)!
    let r = 0
    for (const s of this.subs.values()) if (s.rate > r && s.channels.some((c) => c.id === id)) r = s.rate
    st.rate = Math.min(r, TICK_HZ)
    st.period = st.rate > 0 ? Math.max(1, Math.round(TICK_HZ / st.rate)) : 1
  }
  /** subscribed channels in frame order: roster first, then (priority, id) (AWR-17 §6.4 rule 1) */
  private rebuildActive(): void {
    const w = this.world!
    const out: Channel[] = []
    for (const [id, st] of this.sched) if (st.refs > 0) out.push(w.byId.get(id)!)
    out.sort((x, y) => (x.kind === 'roster' ? -1 : y.kind === 'roster' ? 1 : x.priority - y.priority || x.id - y.id))
    this.active = out
  }

  // ------------------------------------------------------------ events
  queueEvent(e: FakeEvent): void {
    if (!this.eventsOn || !this.hello) return
    const f = this.eventFilter
    if (f && (e.level < f.levelMin || (f.types.length > 0 && !f.types.some((t) => e.type.startsWith(t))))) return
    this.pendingEvents.push(e)
  }
  private replayEvents(after: number): void {
    const w = this.world!
    this.resumeFrom = -1
    const ring = w.ring
    if (ring.length && ring[0].seq > after + 1) {
      this.eventGap = true
      this.sendCtrl({ op: 'status', id: 'events.gap', level: 'warning', message: REASONS[Reason.EVENTS_TRUNCATED].message_zh, source: 'fake' })
    }
    for (const e of ring) if (e.seq > after) this.queueEvent(e)
    this.flushEvents()
  }
  private flushEvents(): void {
    const q = this.pendingEvents
    if (!q.length) return
    this.pendingEvents = []
    for (let i = 0; i < q.length; i += EVENTS_PER_MSG) {
      const chunk = q.slice(i, i + EVENTS_PER_MSG)
      if (chunk.length === 1) this.sendCtrl({ op: 'event', ...chunk[0] })
      else this.sendCtrl({ op: 'events', items: chunk })
    }
  }

  // ------------------------------------------------------------ per-connection tick
  onWorldTick(k: number): void {
    if (this.readyState !== WS_OPEN || !this.hello) return
    this.flushEvents()
    if (k % TIME_EVERY_TICKS === 0 && !this.world!.stalled) this.sendTime()
    this.assemble(k)
  }

  private assemble(k: number): void {
    if (this.sentSeq - this.acked >= this.window) {
      this.stats.creditSkips++
      return
    }
    const w = this.world!
    const due: Channel[] = []
    for (const c of this.active) {
      const st = this.sched.get(c.id)!
      if (st.refs <= 0 || c.seq === 0 || c.payload === null) continue
      if (st.snapshot || ((k % st.period === 0 || st.pending) && c.seq !== st.lastSeq)) due.push(c)
    }
    if (!due.length) return
    let size = 16
    for (const c of due) size += 16 + pad8(c.payload!.byteLength)
    const buf = new ArrayBuffer(size)
    const dv = new DataView(buf)
    const u8 = new Uint8Array(buf)
    let snapshot = false
    for (const c of due) if (this.sched.get(c.id)!.snapshot) snapshot = true
    this.sentSeq++
    dv.setUint8(0, OP_BATCH)
    dv.setUint8(1, (snapshot ? BATCH_SNAPSHOT : 0) | (this.eventGap ? BATCH_GAP : 0))
    this.eventGap = false
    dv.setUint16(2, w.time.epoch, true)
    dv.setUint32(4, this.sentSeq, true)
    setI64(dv, 8, w.tSimNs)
    let off = 16
    for (const c of due) {
      const st = this.sched.get(c.id)!
      const p = c.payload!
      dv.setUint16(off, c.id, true)
      dv.setUint8(off + 2, c.encoding === 'raw' ? ENC_RAW : ENC_MSGPACK)
      dv.setUint8(off + 3, st.snapshot && c.selfContained ? RF_KEYFRAME : 0)
      dv.setUint32(off + 4, p.byteLength, true)
      dv.setUint32(off + 8, c.seq, true)
      dv.setInt32(off + 12, Math.round((c.tSimNs - w.tSimNs) / 1000), true)
      u8.set(p, off + 16)
      off += 16 + pad8(p.byteLength)
      st.lastSeq = c.seq
      st.pending = false
      st.snapshot = false
    }
    this.stats.frames++
    this.emitOut(buf)
  }

  private tickReplay(): void {
    const rec = this.replay!
    const t = this.now() - this.replayBase
    while (this.replayIdx < rec.length && rec[this.replayIdx].t_rel_ns / 1e6 <= t) {
      const r = rec[this.replayIdx++]
      if (r.kind === AWRT_TEXT) this.emitOut(new TextDecoder().decode(r.payload))
      else this.emitOut(r.payload.slice().buffer)
    }
    if (this.replayIdx >= rec.length && this.replayLoopFrom >= 0 && rec.length) {
      // loop the part after the handshake; the next pass starts one tick later
      this.replayIdx = this.replayLoopFrom
      this.replayBase = this.now() - rec[this.replayLoopFrom].t_rel_ns / 1e6 + 1000 / TICK_HZ
    }
  }

  /** replay finished (non-looping) */
  get replayDone(): boolean {
    return !!this.replay && this.replayIdx >= this.replay.length
  }

  /** synthetic vehicle position (tests) */
  positionOf(a: number): [number, number, number] {
    return this.world!.positionOf(a)
  }
}

function advert(c: Channel): Record<string, unknown> {
  const a: Record<string, unknown> = { id: c.id, topic: c.topic, kind: c.kind, encoding: c.encoding, schemaName: c.schemaName,
    nativeHz: c.nativeHz, defaultRate: c.defaultRate, priority: c.priority, selfContained: c.selfContained, entity: c.entity }
  if (c.encoding === 'raw') a.layout = { schemaName: c.schemaName, hash: SCHEMA_HASH[c.schemaName], size: c.schemaName === 'awr.SwarmLite32.v1' ? SL32.SIZE : DS64.SIZE }
  return a
}

/** Factory kept for the M15 skeleton import path. */
export function createFakeSource(o: FakeSourceOptions = {}): FakeSource {
  return new FakeSource(o)
}
