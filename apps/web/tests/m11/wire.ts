// Wire-level helpers for the M11 client tests: a scripted socket the test drives frame by frame, and builders for TIME,
// BATCH, serverInfo and advertise with the generated layouts (AWR-17 §6.2-§6.5). No Math.random (DET-01).
import * as L from '@awr/contracts/layouts'
import { OP_BATCH, pad8 } from '@awr/contracts/frame'
import { writeTime } from '@awr/contracts/time'
import { FrameFront as FrameFrontCtor, SLOT_BYTES } from '@/net/rt/frame'
import { RtHost, type CtrlMsg, type HostEnv, type WorkerOut } from '@/net/rt/session'
import { WS_CLOSED, WS_CONNECTING, WS_OPEN, type SocketClose, type SocketLike, type SocketMessage } from '@/net/rt/transport'

/** SocketLike driven by the test: open() then serverSend(); client sends are recorded */
export class ScriptSocket implements SocketLike {
  binaryType = 'arraybuffer'
  readyState = WS_CONNECTING
  onopen: ((ev?: unknown) => void) | null = null
  onmessage: ((ev: SocketMessage) => void) | null = null
  onclose: ((ev: SocketClose) => void) | null = null
  onerror: ((ev?: unknown) => void) | null = null
  readonly sent: (string | ArrayBuffer)[] = []
  /** false: client sends are counted but not kept (allocation tests) */
  record = true
  sentCount = 0
  constructor(readonly url: string, readonly protocols: string[]) {}
  open(): void {
    this.readyState = WS_OPEN
    this.onopen?.()
  }
  send(data: string | ArrayBuffer | ArrayBufferView): void {
    this.sentCount++
    if (!this.record) return
    if (typeof data === 'string') this.sent.push(data)
    else if (data instanceof ArrayBuffer) this.sent.push(data.slice(0))
    else this.sent.push(new Uint8Array(data.buffer, data.byteOffset, data.byteLength).slice().buffer)
  }
  close(code = 1000): void {
    if (this.readyState === WS_CLOSED) return
    this.readyState = WS_CLOSED
    this.onclose?.({ code })
  }
  serverSend(data: string | ArrayBuffer | Record<string, unknown>): void {
    const d = typeof data === 'string' || data instanceof ArrayBuffer ? data : JSON.stringify(data)
    this.onmessage?.({ data: d })
  }
  serverClose(code: number): void {
    this.readyState = WS_CLOSED
    this.onclose?.({ code })
  }
  json(): Record<string, unknown>[] {
    return this.sent.filter((x): x is string => typeof x === 'string').map((x) => JSON.parse(x) as Record<string, unknown>)
  }
  ops(op: string): Record<string, unknown>[] {
    return this.json().filter((m) => m.op === op)
  }
}

export function timeFrame(o: { state?: number; replay?: boolean; epoch: number; rate?: number; tSimNs: number; tSrvNs: number }): ArrayBuffer {
  const b = new ArrayBuffer(24)
  writeTime(new DataView(b), 0, { state: o.state ?? 9, replay: o.replay ?? false, epoch: o.epoch, rate: o.rate ?? 1, t_sim_ns: o.tSimNs, t_srv_ns: o.tSrvNs })
  return b
}

export interface Rec { ch: number; enc?: number; rflags?: number; seq: number; dtUs?: number; payload: Uint8Array }
export function batchFrame(o: { flags?: number; epoch: number; frameSeq: number; tSimNs: number; records: Rec[] }): ArrayBuffer {
  let size = 16
  for (const r of o.records) size += 16 + pad8(r.payload.byteLength)
  const b = new ArrayBuffer(size)
  const dv = new DataView(b)
  const u8 = new Uint8Array(b)
  dv.setUint8(0, OP_BATCH)
  dv.setUint8(1, o.flags ?? 0)
  dv.setUint16(2, o.epoch, true)
  dv.setUint32(4, o.frameSeq, true)
  L.setI64(dv, 8, o.tSimNs)
  let off = 16
  for (const r of o.records) {
    dv.setUint16(off, r.ch, true)
    dv.setUint8(off + 2, r.enc ?? 0)
    dv.setUint8(off + 3, r.rflags ?? 0)
    dv.setUint32(off + 4, r.payload.byteLength, true)
    dv.setUint32(off + 8, r.seq, true)
    dv.setInt32(off + 12, r.dtUs ?? 0, true)
    u8.set(r.payload, off + 16)
    off += 16 + pad8(r.payload.byteLength)
  }
  return b
}

/** n Lite32 rows; row(a) gives the fields of vehicle a */
export function lite32(n: number, row: (a: number) => Partial<L.SwarmLite32Rec> = () => ({})): Uint8Array {
  const u8 = new Uint8Array(L.SL32.SIZE * n)
  const dv = new DataView(u8.buffer)
  for (let a = 0; a < n; a++) {
    const r: L.SwarmLite32Rec = { ...L.newSwarmLite32(), agent_no: a, flight_state: 5, battery_pct: 80, flags: 0x37, ctrl: 0x61,
      pos: [a, 2 * a, 30 + a], q_snorm: [0, 0, 0, 32767], vel_cms: [100, -50, 25], ...row(a) }
    L.writeSwarmLite32(dv, L.SL32.SIZE * a, r)
  }
  return u8
}

export function full64(agentNo: number, pos: [number, number, number], extra: Partial<L.DroneState64Rec> = {}): Uint8Array {
  const u8 = new Uint8Array(L.DS64.SIZE)
  L.writeDroneState64(new DataView(u8.buffer), 0, { ...L.newDroneState64(), agent_no: agentNo, flight_state: 5, flags: 0x37, mission_item: 0xffff,
    battery_pct: 70, ctrl: 0x61, pos, vel: [1, 2, 3], q: [0, 0, 0, 1], omega: [0, 0, 0], dt_us: 0, ...extra })
  return u8
}

export function serverInfo(o: { sessionId?: string; layouts?: Record<string, string>; contracts?: string; role?: string } = {}): Record<string, unknown> {
  const layouts: Record<string, string> = o.layouts ?? {}
  if (!o.layouts) for (const s of ['awr.SwarmLite32.v1', 'awr.DroneState64.v1', 'awr.rt.Time.v1']) layouts[s] = L.SCHEMA_HASH[s]
  return { op: 'serverInfo', name: 'awr-gateway', protocol: 'awr.rt.v1', sessionId: o.sessionId ?? 'gw-1', connId: 'c-1', capabilities: ['time', 'credit', 'rpc', 'events'],
    window: 6, tickHz: 60, rateClasses: [1, 2, 5, 10, 15, 20, 30, 60], world: { id: 'shenzhen', frame: 'world' }, run: { id: 'r1', segment: 0 }, mode: 'live',
    clock: { mode: 'lockstep', pausable: true, max_speed: 20, steppable: true }, role: o.role ?? 'operator', principal: 'p-x', seat: 'held',
    contracts: o.contracts ?? L.CONTRACTS_VERSION, layouts }
}

export const CH = { roster: 1, swarm: 2, env: 3, full0: 16, full1: 17, envSample: 40, pose: 41, unknownEnc: 50 } as const
export function advertise(): Record<string, unknown> {
  const ch = (id: number, topic: string, encoding: string, schemaName: string, entity: string | null = null): Record<string, unknown> =>
    ({ id, topic, kind: 'state', encoding, schemaName, nativeHz: 10, defaultRate: 10, priority: 1, selfContained: false, entity: entity ? { kind: 'uav', id: entity } : null })
  return { op: 'advertise', channels: [
    ch(CH.roster, 'fleet/roster', 'msgpack', 'awr.fleet.roster.v1'), ch(CH.swarm, 'swarm/uav/state', 'raw', 'awr.SwarmLite32.v1'),
    ch(CH.env, 'env/state', 'msgpack', 'awr.env.keyframe.v1'), ch(CH.full0, 'uav/u0/state', 'raw', 'awr.DroneState64.v1', 'u0'),
    ch(CH.full1, 'uav/u1/state', 'raw', 'awr.DroneState64.v1', 'u1'), ch(CH.envSample, 'uav/u1/env', 'raw', 'awr.EnvSample32.v1', 'u1'),
    ch(CH.pose, 'uav/u0/sensor/cam/pose', 'raw', 'awr.SensorPose48.v1', 'u0'), ch(CH.unknownEnc, 'mystery/x', 'blob', 'awr.blob.x.v1'),
  ] }
}

export interface ScriptRig {
  host: RtHost
  sock: ScriptSocket
  socks: ScriptSocket[]
  clock: { t: number }
  ctrl: CtrlMsg[]
  /** last posted slot (returned on the next pull) */
  held: ArrayBuffer | null
  /** copy of the most recently posted slot (null before the first) */
  last: ArrayBuffer | null
  /** slots posted so far */
  slotCount: number
  posts: number
  pull(): void
  timers: { fn: () => void; at: number }[]
  advance(ms: number): void
  /** run the earliest timer due within ms (moving the clock to it); false when none */
  nextTimer(ms: number): boolean
}

/** RtHost over ScriptSockets with a manual clock; the socket opens on connect and the handshake is left to the test */
export function scriptRig(o: { url?: string; checkAuth?: HostEnv['checkAuth']; timeOrigin?: number; timeOriginMain?: number; keepSlots?: boolean } = {}): ScriptRig {
  const clock = { t: 1000 }
  const timers: { fn: () => void; at: number }[] = []
  const socks: ScriptSocket[] = []
  const r = { clock, timers, socks, ctrl: [] as CtrlMsg[], held: null as ArrayBuffer | null, last: null as ArrayBuffer | null, slotCount: 0, posts: 0 } as unknown as ScriptRig
  const env: HostEnv = {
    post: (m: WorkerOut) => {
      r.posts++
      if (m.slot) {
        r.held = m.slot
        r.last = o.keepSlots === false ? null : m.slot.slice(0)
        r.slotCount++
      }
      if (m.ctrl) r.ctrl.push(...m.ctrl)
    },
    socket: (url, protocols) => {
      const s = new ScriptSocket(url, protocols)
      socks.push(s)
      return s
    },
    now: () => clock.t,
    timeOrigin: o.timeOrigin ?? 0,
    random: () => 0.5,
    setTimeout: (fn, ms) => {
      const h = { fn, at: clock.t + ms }
      timers.push(h)
      return h
    },
    clearTimeout: (h) => {
      const i = timers.indexOf(h as { fn: () => void; at: number })
      if (i >= 0) timers.splice(i, 1)
    },
    checkAuth: o.checkAuth,
  }
  r.host = new RtHost(env)
  Object.defineProperty(r, 'sock', { get: () => socks[socks.length - 1] })
  r.pull = () => {
    const ret = r.held
    r.held = null
    r.host.onMessage({ cmd: 'pull', returned: ret })
  }
  r.advance = (ms: number) => {
    const end = clock.t + ms
    for (;;) {
      let next: { fn: () => void; at: number } | null = null
      for (const t of timers) if (t.at <= end && (!next || t.at < next.at)) next = t
      if (!next) break
      clock.t = Math.max(clock.t, next.at)
      timers.splice(timers.indexOf(next), 1)
      next.fn()
    }
    clock.t = end
  }
  r.nextTimer = (ms: number) => {
    let next: { fn: () => void; at: number } | null = null
    for (const t of timers) if (t.at <= clock.t + ms && (!next || t.at < next.at)) next = t
    if (!next) return false
    clock.t = Math.max(clock.t, next.at)
    timers.splice(timers.indexOf(next), 1)
    next.fn()
    return true
  }
  r.host.onMessage({ cmd: 'init', url: o.url ?? 'ws://127.0.0.1:8000/api/rt', token: 'tok', tier: 'S', deviceClass: 'software',
    timeOriginMain: o.timeOriginMain ?? 0, slots: [new ArrayBuffer(SLOT_BYTES), new ArrayBuffer(SLOT_BYTES), new ArrayBuffer(SLOT_BYTES)] })
  return r
}

/** FrameFront over the most recently posted slot */
export function lastFrame(r: ScriptRig): import('@/net/rt/frame').FrameFront {
  const f = new FrameFrontCtor()
  if (r.last) f.load(r.last)
  return f
}

/** open the current socket and run the handshake: serverInfo, advertise, TIME(epoch) */
export function handshake(r: ScriptRig, o: { epoch?: number; sessionId?: string } = {}): void {
  r.sock.open()
  r.sock.serverSend(serverInfo({ sessionId: o.sessionId }))
  r.sock.serverSend(advertise())
  r.sock.serverSend(timeFrame({ epoch: o.epoch ?? 1, tSimNs: 1e9, tSrvNs: 2e9 }))
}

/** deliver the queued control messages (a pull flushes them at once) and return those with op */
export function ctrlOf(r: ScriptRig, op: string): CtrlMsg[] {
  r.pull()
  return r.ctrl.filter((m) => m.op === op)
}
/** the latest `conn` notice after delivering the queue */
export function connOf(r: ScriptRig): CtrlMsg {
  return ctrlOf(r, 'conn').at(-1)!
}
